import json

from twisted.internet.defer import inlineCallbacks, returnValue, DeferredQueue
from twisted.web.server import NOT_DONE_YET
from twisted.web import http

from vumi.tests.utils import LogCatcher
from vumi.tests.helpers import VumiTestCase
from vumi.tests.fake_connection import FakeHttpServer
from vumi.transports.httprpc.tests.helpers import HttpRpcTransportHelper

from vxtelegram.telegram import TelegramTransport


class TestTelegramTransport(VumiTestCase):

    # Default configurations for our bot
    bot_username = '@bot'
    API_URL = 'https://api.telegram.org/bot'
    TOKEN = '1234'

    # Telegram chat types
    PRIVATE = 'private'
    CHANNEL = 'channel'
    GROUP = 'group'

    # Telegram usernames are human-readable strings that identify users
    TELEGRAM_USERNAME = 'telegram_username'
    # Telegram ids are integers that identify users in the Telegram API
    TELEGRAM_ID = 'telegram_id'

    # Some default Telegram objects
    default_user = {
        'id': 2468,
        'username': '@default_user',
    }
    bad_telegram_response = {
        'ok': False,
        'description': 'Bad request',
    }

    def setUp(self):
        self.helper = self.add_helper(
            HttpRpcTransportHelper(TelegramTransport)
        )
        self.request_queue = DeferredQueue()
        self.pending_requests = []
        self.addCleanup(self.finish_requests)
        self.mock_server = FakeHttpServer(self.handle_inbound_request)

    @inlineCallbacks
    def get_transport(self, **config):
        defaults = {
            'bot_username': self.bot_username,
            'bot_token': self.TOKEN,
            'web_path': 'foo',
            'web_port': 0,
            'inbound_url': 'www.example.com',
            'outbound_url': self.API_URL,
        }
        defaults.update(config)
        transport = yield self.helper.get_transport(defaults)
        transport.agent_factory = self.mock_server.get_agent
        returnValue(transport)

    def handle_inbound_request(self, req):
        self.request_queue.put(req)
        return NOT_DONE_YET

    @inlineCallbacks
    def get_next_request(self):
        req = yield self.request_queue.get()
        self.pending_requests.append(req)
        returnValue(req)

    @inlineCallbacks
    def finish_requests(self):
        for req in self.pending_requests:
            if not req.finished:
                yield req.finish()

    @inlineCallbacks
    def test_starting_status(self):
        """
        Our transport should publish a 'down' status while setting up.
        """
        yield self.get_transport(publish_status=True)

        # Ignore status published during webhook setup
        [status, _] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'down',
            'component': 'telegram_setup',
            'type': 'starting',
            'message': 'Telegram transport starting...',
        })

    @inlineCallbacks
    def test_get_outbound_url(self):
        """
        Our helper method for building outbound URLs should build URLs using
        the Telegram API URL, our bot token, and the applicable method (path).
        """
        transport = yield self.get_transport()
        test_url = transport.get_outbound_url('myPath')
        expected_url = '%s%s/%s' % (self.API_URL, self.TOKEN, 'myPath')
        self.assertEqual(test_url, expected_url)

    @inlineCallbacks
    def test_setup_webhook_no_errors(self):
        """
        We should log successful webhook setup and our request should be
        in the proper format. We should also publish an 'ok' status.
        """
        transport = yield self.get_transport(publish_status=True)
        d = transport.setup_webhook()
        expected_url = '%s%s/%s' % (self.API_URL.rstrip('/'), self.TOKEN,
                                    'setWebhook')

        req = yield self.get_next_request()
        self.assertEqual(req.method, 'POST')
        self.assertEqual(req.path, expected_url)

        content = json.load(req.content)
        self.assertEqual(content['url'], 'www.example.com')

        req.write(json.dumps({'ok': True}))
        with LogCatcher(message='Webhook') as lc:
            req.finish()
            yield d
            [log] = lc.messages()
            self.assertEqual(log, 'Webhook set up on www.example.com')

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'ok',
            'component': 'telegram_webhook',
            'type': 'webhook_setup_success',
            'message': 'Webhook setup successful',
            'details': {'webhook_url': 'www.example.com'},
        })

    @inlineCallbacks
    def test_setup_webhook_with_errors(self):
        """
        We should log error messages received during webhook setup and publish
        a 'down' status.
        """
        error = self.bad_telegram_response['description']
        transport = yield self.get_transport(publish_status=True)
        d = transport.setup_webhook()

        req = yield self.get_next_request()
        req.setResponseCode(http.BAD_REQUEST)
        req.write(json.dumps(self.bad_telegram_response))
        with LogCatcher(message='Webhook') as lc:
            req.finish()
            yield d
            [log] = lc.messages()
            self.assertEqual(
                log,
                'Webhook setup failed: bad response from Telegram',
            )

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'down',
            'component': 'telegram_webhook',
            'type': 'bad_response',
            'message': 'Webhook setup failed: bad response from Telegram',
            'details': {'error': error, 'res_code': 400},
        })

    @inlineCallbacks
    def test_setup_webhook_with_redirect(self):
        """
        We should log cases where our request to set up a webhook is redirected
        and publish a 'down' status.
        """
        transport = yield self.get_transport(publish_status=True)
        d = transport.setup_webhook()
        req = yield self.get_next_request()

        req.setResponseCode(http.FOUND)
        req.redirect('www.redirected.com')
        with LogCatcher(message='Webhook') as lc:
            req.finish()
            yield d
            [log] = lc.messages()
            self.assertEqual(
                log,
                'Webhook setup failed: request redirected',
            )

        # Ignore statuses published on transport startup (only one, since
        # during tests this status is already published and doesn't repeat)
        [_, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'down',
            'component': 'telegram_webhook',
            'type': 'request_redirected',
            'message': 'Webhook setup failed: request redirected',
            'details': {'error': 'Unexpected redirect', 'res_code': 302},
        })

    @inlineCallbacks
    def test_setup_webhook_with_unexpected_response(self):
        """
        We should log cases where our request to set up a webhook receives a
        response that isn't JSON and publish a 'down' status.
        """
        transport = yield self.get_transport(publish_status=True)
        d = transport.setup_webhook()
        req = yield self.get_next_request()

        req.setResponseCode(http.INTERNAL_SERVER_ERROR)
        req.write("This isn't JSON!")
        with LogCatcher(message='Webhook') as lc:
            req.finish()
            yield d
            [log] = lc.messages()
            self.assertEqual(
                log,
                'Webhook setup failed: unexpected response format',
            )

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'down',
            'component': 'telegram_webhook',
            'type': 'unexpected_response_format',
            'message': 'Webhook setup failed: unexpected response format',
        })
        self.assertEqual(status['details']['res_code'], 500)
        self.assertEqual(status['details']['res_body'], "This isn't JSON!")

    @inlineCallbacks
    def test_translate_inbound_message_from_channel(self):
        """
        When translating a message from a channel into Vumi's preferred format,
        we should use the channel's chat id as from_addr.
        """
        transport = yield self.get_transport()
        default_channel = {
            'id': 2468,
            'type': self.CHANNEL,
        }
        inbound_msg = {
            'message_id': 1234,
            'chat': default_channel,
            'text': 'Hi from Telegram channel!',
        }

        message = transport.translate_inbound_message(inbound_msg)
        self.assert_dict(message, {
            'content': inbound_msg['text'],
            'from_addr': default_channel['id'],
            'telegram_msg_id': inbound_msg['message_id'],
            'telegram_username': None,
        })

    @inlineCallbacks
    def test_translate_inbound_message_from_user(self):
        """
        We should translate a Telegram message object into a Vumi message.
        """
        transport = yield self.get_transport()
        inbound_msg = {
            'message_id': 1234,
            'chat': {},
            'text': 'Hi from Telegram user!',
            'from': self.default_user,
        }

        message = transport.translate_inbound_message(inbound_msg)
        self.assert_dict(message, {
            'content': inbound_msg['text'],
            'from_addr': self.default_user['id'],
            'telegram_msg_id': inbound_msg['message_id'],
            'telegram_username': self.default_user['username'],
        })

    @inlineCallbacks
    def test_update_lifetime(self):
        """
        update_ids in Redis should expire after update_lifetime has elapsed,
        meaning they should no longer be considered duplicates.
        """
        transport = yield self.get_transport(update_lifetime=10)
        yield transport.mark_as_seen(1234)

        duplicate = yield transport.is_duplicate(1234)
        self.assertTrue(duplicate)
        ttl = yield transport.redis.ttl(1234)
        self.assertTrue(ttl <= 10)

    @inlineCallbacks
    def test_duplicate_update(self):
        """
        We should log receipt of duplicate updates and discard them.
        """
        yield self.get_transport()
        update = {'update_id': 1234}

        # Make initial request
        d = self.helper.mk_request(_method='POST', _data=json.dumps(update))
        with LogCatcher(message='message') as lc:
            res = yield d
            [log] = lc.messages()
            self.assertEqual(log, 'Inbound update does not contain a message')
        self.assertEqual(res.code, http.OK)

        # Make duplicate request
        d = self.helper.mk_request(_method='POST', _data=json.dumps(update))
        with LogCatcher(message='duplicate') as lc:
            res = yield d
            [log] = lc.messages()
            self.assertEqual(log, 'Received a duplicate update: 1234')
        self.assertEqual(res.code, http.OK)

    @inlineCallbacks
    def test_inbound_update(self):
        """
        We should be able to receive updates from Telegram and publish them
        as Vumi messages, as well as log the receipt / publish an 'ok' status.
        """
        transport = yield self.get_transport(publish_status=True)
        expected_log = (
            'TelegramTransport receiving inbound message from %s to %s' %
            (self.default_user['username'], self.bot_username)
        )
        update = {
            'update_id': 1234,
            'message': {
                'message_id': 5678,
                'from': self.default_user,
                'chat': {'id': 'chat_id', 'type': self.PRIVATE},
                'date': 1234,
                'text': 'Incoming message from Telegram!',
            },
        }

        d = self.helper.mk_request(_method='POST', _data=json.dumps(update))
        with LogCatcher(message='TelegramTransport') as lc:
            res = yield d
            [log] = lc.messages()
            self.assertEqual(log, expected_log)
        self.assertEqual(res.code, http.OK)

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'ok',
            'component': 'telegram_inbound',
            'type': 'good_inbound',
            'message': 'Good inbound request',
        })

        [msg] = yield self.helper.wait_for_dispatched_inbound(1)
        self.assert_dict(msg, {
            'to_addr': self.bot_username,
            'to_addr_type': self.TELEGRAM_USERNAME,
            'from_addr': self.default_user['id'],
            'from_addr_type': self.TELEGRAM_ID,
            'content': update['message']['text'],
            'transport_type': transport.transport_type,
            'transport_name': transport.transport_name,
            'helper_metadata': {'telegram': {
                'telegram_username': self.default_user['username'],
            }},
            'transport_metadata': {
                'telegram_msg_id': update['message']['message_id'],
                'telegram_username': self.default_user['username'],
            },
        })

    @inlineCallbacks
    def test_inbound_inline_query(self):
        """
        We should be able to receive inline queries from Telegram and publish
        them as Vumi messages, as well as log the receipt and update status.
        """
        transport = yield self.get_transport(publish_status=True)
        expected_log = (
            'TelegramTransport receiving inline query from %s to %s' %
            (self.default_user['username'], self.bot_username)
        )
        update = {
            'update_id': 1234,
            'inline_query': {
                'id': "1234",
                'from': self.default_user,
                'query': 'Do something, bot!',
            },
        }

        d = self.helper.mk_request(_method='POST', _data=json.dumps(update))
        with LogCatcher(message='inline') as lc:
            res = yield d
            [log] = lc.messages()
            self.assertEqual(log, expected_log)
        self.assertEqual(res.code, http.OK)

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'ok',
            'component': 'telegram_inbound',
            'type': 'good_inbound',
            'message': 'Good inbound request',
        })

        [msg] = yield self.helper.wait_for_dispatched_inbound(1)
        self.assert_dict(msg, {
            'content': update['inline_query']['query'],
            'to_addr': self.bot_username,
            'to_addr_type': self.TELEGRAM_USERNAME,
            'from_addr': self.default_user['id'],
            'from_addr_type': self.TELEGRAM_ID,
            'transport_type': transport.transport_type,
            'transport_name': transport.transport_name,
            'helper_metadata': {'telegram': {
                'type': 'inline_query',
                'details': {'inline_query_id': update['inline_query']['id']},
                'telegram_username': self.default_user['username'],
            }},
            'transport_metadata': {
                'type': 'inline_query',
                'details': {'inline_query_id': update['inline_query']['id']},
                'telegram_username': self.default_user['username'],
            },
        })

    @inlineCallbacks
    def test_inbound_non_message_update(self):
        """
        We should log receipt of non-message updates and discard them.
        """
        yield self.get_transport()
        update = json.dumps({
            'update_id': 1234,
            'object': 'This is not a message...',
        })

        d = self.helper.mk_request(_method='POST', _data=update)
        with LogCatcher(message='message') as lc:
            res = yield d
            [log] = lc.messages()
            self.assertEqual(log, 'Inbound update does not contain a message')
        self.assertEqual(res.code, http.OK)

    @inlineCallbacks
    def test_inbound_non_text_message(self):
        """
        We should log receipt of non-text messages and discard them.
        """
        yield self.get_transport()
        update = json.dumps({
            'update_id': 1234,
            'message': {
                'message_id': 5678,
                'object': 'This is not a text message...',
            },
        })

        d = self.helper.mk_request(_method='POST', _data=update)
        with LogCatcher(message='text') as lc:
            res = yield d
            [log] = lc.messages()
            self.assertEqual(log, 'Inbound message is not a text message')
        self.assertEqual(res.code, http.OK)

    @inlineCallbacks
    def test_inbound_update_unexpected_format(self):
        """
        We should log a warning and publish a down status when we receive
        updates that aren't in JSON format.
        """
        yield self.get_transport(publish_status=True)

        d = self.helper.mk_request(_method='POST', _data="This isn't JSON!")
        with LogCatcher(message='unexpected') as lc:
            res = yield d
            [log] = lc.messages()
            self.assertSubstring('Inbound update in unexpected format', log)
        self.assertEqual(res.code, http.BAD_REQUEST)

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'down',
            'component': 'telegram_inbound',
            'type': 'unexpected_update_format',
            'message': 'Inbound update in unexpected format',
        })
        self.assertEqual(status['details']['req_content'], "This isn't JSON!")

    @inlineCallbacks
    def test_outbound_query_reply_no_errors(self):
        """
        We should be able to reply to inline queries using POST requests, and
        publish an ack and an 'ok' status when we receive a positive response.
        """
        yield self.get_transport(publish_status=True)
        expected_url = '%s%s/%s' % (self.API_URL.rstrip('/'), self.TOKEN,
                                    'answerInlineQuery')

        results = [{
            'type': 'article',
            'url': 'www.example.com',
            'title': 'Example',
            'id': '12345678',
            'input_message_content': {'message_text': 'Hello, world!'},
        }]
        msg = self.helper.make_outbound(
            content=None,
            to_addr=self.default_user['username'],
            from_addr_type=self.TELEGRAM_USERNAME,
            from_addr=self.bot_username,
            transport_metadata={
                'type': 'inline_query',
                'details': {'inline_query_id': '1234'},
                'telegram_user_id': self.default_user['id'],
            },
            helper_metadata={
                'telegram': {'type': 'inline_query_reply', 'results': results},
            },
        )
        d = self.helper.dispatch_outbound(msg)

        req = yield self.get_next_request()
        self.assertEqual(req.method, 'POST')
        self.assertEqual(req.path, expected_url)

        outbound_msg = json.load(req.content)
        self.assertEqual(outbound_msg['inline_query_id'], '1234')
        self.assertEqual(outbound_msg['results'], results)

        req.write(json.dumps({'ok': True}))
        req.finish()
        yield d

        self.assert_ack(msg['message_id'])

        # Ignore statuses published on transport startup
        [_, __, out, query] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(out, {
            'status': 'ok',
            'component': 'telegram_outbound',
            'type': 'good_outbound_request',
            'message': 'Outbound request successful',
        })
        self.assert_dict(query, {
            'status': 'ok',
            'component': 'telegram_query_reply',
            'type': 'good_query_reply',
            'message': 'Outbound request successful',
        })

    @inlineCallbacks
    def test_outbound_query_reply_unexpected_format(self):
        """
        We should log and publish a nack / 'down' status when a query reply
        does not contain a results field.
        """
        yield self.get_transport(publish_status=True)
        expected_log = 'Query reply not sent: results field not present'
        msg = self.helper.make_outbound(
            content=None,
            to_addr=self.default_user['username'],
            to_addr_type=self.TELEGRAM_USERNAME,
            from_addr=self.bot_username,
            transport_metadata={
                'type': 'inline_query',
                'details': {'inline_query_id': 'valid_id'},
                'telegram_user_id': self.default_user['id'],
            },
            helper_metadata={'telegram': {}},
        )

        d = self.helper.dispatch_outbound(msg)
        with LogCatcher(message='results') as lc:
            yield d
            [log] = lc.messages()
            self.assertEqual(log, expected_log)
            self.assert_nack(msg['message_id'], expected_log)

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'down',
            'component': 'telegram_query_reply',
            'type': 'bad_query_reply',
            'message': 'Query reply not sent: results field not present',
        })
        # We assert this field separately as a substring because it is verbose
        self.assertSubstring(
            "you should disable your bot's inline mode",
            status['details']['error']
        )

    @inlineCallbacks
    def test_outbound_query_reply_with_errors(self):
        """
        We should publish a nack and a 'down' status when we receive an error
        response from Telegram while trying to reply to an inline query.
        """
        yield self.get_transport(publish_status=True)
        msg = self.helper.make_outbound(
            content=None,
            to_addr=self.default_user['username'],
            to_addr_type=self.TELEGRAM_USERNAME,
            from_addr=self.bot_username,
            transport_metadata={
                'type': 'inline_query',
                'details': {'inline_query_id': 'invalid_id'},
                'telegram_user_id': self.default_user['id'],
            },
            helper_metadata={
                'telegram': {'type': 'inline_query_reply', 'results': []},
            },
        )
        d = self.helper.dispatch_outbound(msg)

        req = yield self.get_next_request()
        req.setResponseCode(http.BAD_REQUEST)
        req.write(json.dumps({'ok': False, 'description': 'INVALID_QUERY_ID'}))
        req.finish()
        yield d

        yield self.assert_nack(
            msg['message_id'],
            'Query reply not sent: bad response from Telegram',
        )

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'down',
            'component': 'telegram_outbound',
            'type': 'bad_response',
            'message': 'Query reply not sent: bad response from Telegram',
            'details': {
                'error': 'INVALID_QUERY_ID',
                'res_code': 400,
                'inline_query_id': 'invalid_id',
            },
        })

    @inlineCallbacks
    def test_outbound_message_no_errors(self):
        """
        We should be able to send a message to Telegram as a POST request, and
        publish an ack and an 'ok' status when we receive a positive response.
        """
        yield self.get_transport(publish_status=True)
        expected_url = '%s%s/%s' % (self.API_URL.rstrip('/'), self.TOKEN,
                                    'sendMessage')
        msg = self.helper.make_outbound(
            content='Outbound message!',
            to_addr=self.default_user['username'],
            to_addr_type=self.TELEGRAM_USERNAME,
            from_addr=self.bot_username,
            transport_metadata={'telegram_user_id': self.default_user['id']},
        )
        d = self.helper.dispatch_outbound(msg)

        req = yield self.get_next_request()
        self.assertEqual(req.method, 'POST')
        self.assertEqual(req.path, expected_url)

        outbound_msg = json.load(req.content)
        self.assert_dict(outbound_msg, {
            'text': msg['content'],
            'chat_id': msg['to_addr'],
        })

        req.write(json.dumps({'ok': True}))
        req.finish()
        yield d

        yield self.assert_ack(msg['message_id'])

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'ok',
            'component': 'telegram_outbound',
            'type': 'good_outbound_request',
            'message': 'Outbound request successful',
        })

    @inlineCallbacks
    def test_outbound_reply_no_errors(self):
        """
        We should be able to reply to messages using the original message's
        Telegram message_id (we don't assert statuses here, since we
        already test that in test_outbound_message_no_errors).
        """
        yield self.get_transport()
        expected_url = '%s%s/%s' % (self.API_URL.rstrip('/'), self.TOKEN,
                                    'sendMessage')
        msg = self.helper.make_outbound(
            content='Outbound reply!',
            to_addr=self.default_user['username'],
            to_addr_type=self.TELEGRAM_USERNAME,
            from_addr=self.bot_username,
            in_reply_to=2468,
            transport_metadata={
                'telegram_msg_id': 1234,
                'telegram_user_id': 36912,
            },
        )
        d = self.helper.dispatch_outbound(msg)

        req = yield self.get_next_request()
        self.assertEqual(req.method, 'POST')
        self.assertEqual(req.path, expected_url)

        outbound_msg = json.load(req.content)
        self.assert_dict(outbound_msg, {
            'text': msg['content'],
            'chat_id': msg['to_addr'],
            'reply_to_message': msg['transport_metadata']['telegram_msg_id'],
        })

        req.write(json.dumps({'ok': True}))
        req.finish()
        yield d

        yield self.assert_ack(msg['message_id'])

    @inlineCallbacks
    def test_outbound_message_with_errors(self):
        """
        We should publish a nack and a 'down' status when we get an error
        response from Telegram while trying to send a message.
        """
        yield self.get_transport(publish_status=True)
        msg = yield self.helper.make_outbound(
            content='Outbound message!',
            to_addr=self.default_user['username'],
            transport_metadata={'telegram_user_id': 1234},
        )
        d = self.helper.dispatch_outbound(msg)

        req = yield self.get_next_request()
        req.setResponseCode(http.BAD_REQUEST)
        req.write(json.dumps(self.bad_telegram_response))
        req.finish()
        yield d

        yield self.assert_nack(msg['message_id'],
                               'Message not sent: bad response from Telegram')

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'down',
            'component': 'telegram_outbound',
            'type': 'bad_response',
            'message': 'Message not sent: bad response from Telegram',
            'details': {
                'error': self.bad_telegram_response['description'],
                'res_code': 400,
            },
        })

    @inlineCallbacks
    def test_outbound_message_with_unexpected_response(self):
        """
        We should publish a nack and a 'down' status when our request to
        Telegram gets a response that isn't JSON.
        """
        yield self.get_transport(publish_status=True)
        msg = yield self.helper.make_outbound(
            content='Outbound message!',
            to_addr=self.default_user['username'],
            transport_metadata={'telegram_user_id': 1234},
        )
        d = self.helper.dispatch_outbound(msg)

        req = yield self.get_next_request()
        req.setResponseCode(http.INTERNAL_SERVER_ERROR)
        req.write("This isn't JSON!")
        req.finish()
        yield d

        yield self.assert_nack(msg['message_id'],
                               'Message not sent: unexpected response format')

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'down',
            'component': 'telegram_outbound',
            'type': 'unexpected_response_format',
            'message': 'Message not sent: unexpected response format',
        })
        self.assertEqual(status['details']['res_code'], 500)
        self.assertEqual(status['details']['res_body'], "This isn't JSON!")

    @inlineCallbacks
    def test_outbound_message_with_redirect(self):
        """
        We should publish a nack and a 'down' status when our request to
        Telegram is redirected.
        """
        yield self.get_transport(publish_status=True)
        msg = yield self.helper.make_outbound(
            content='Outbound message!',
            to_addr=self.default_user['username'],
            transport_metadata={'telegram_user_id': 1234},
        )
        d = self.helper.dispatch_outbound(msg)

        req = yield self.get_next_request()
        req.setResponseCode(http.FOUND)
        req.redirect('www.redirected.com')
        req.finish()
        yield d

        yield self.assert_nack(msg['message_id'],
                               'Message not sent: request redirected')

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'down',
            'component': 'telegram_outbound',
            'type': 'request_redirected',
            'message': 'Message not sent: request redirected',
            'details': {'error': 'Unexpected redirect', 'res_code': 302},
        })

    @inlineCallbacks
    def test_outbound_failure(self):
        """
        outbound_failure is a helper method for our transport that should both
        publish a 'down' status and a nack with the correct reason.
        """
        transport = yield self.get_transport(publish_status=True)
        error = self.bad_telegram_response['description']
        yield transport.outbound_failure(
            status_type='test',
            message_id='id',
            message='Some kind of error',
            details={'error': error},
        )
        yield self.assert_nack('id', 'Some kind of error')

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'down',
            'component': 'telegram_outbound',
            'type': 'test',
            'message': 'Some kind of error',
            'details': {'error': error},
        })

    @inlineCallbacks
    def test_outbound_success(self):
        """
        outbound_success is a helper method for our transport that should both
        publish an 'ok' status and an ack.
        """
        transport = yield self.get_transport(publish_status=True)
        yield transport.outbound_success(message_id='id')
        yield self.assert_ack('id')

        # Ignore statuses published on transport startup
        [_, __, status] = yield self.helper.wait_for_dispatched_statuses()
        self.assert_dict(status, {
            'status': 'ok',
            'component': 'telegram_outbound',
            'type': 'good_outbound_request',
            'message': 'Outbound request successful',
        })

    def assert_dict(self, dictionary, expected_fields):
        """
        Helper method for asserting that a dict contains the expected fields.
        """
        for key in expected_fields.keys():
            self.assertEqual(dictionary[key], expected_fields[key])

    @inlineCallbacks
    def assert_nack(self, message_id, reason):
        """
        Helper method for asserting that nacks are published correctly.
        """
        [nack] = yield self.helper.wait_for_dispatched_events(1)
        self.assertEqual(nack['event_type'], 'nack')
        self.assertEqual(nack['user_message_id'], message_id)
        self.assertEqual(reason, nack['nack_reason'])

    @inlineCallbacks
    def assert_ack(self, message_id):
        """
        Helper method for asserting that acks are published correctly.
        """
        [ack] = yield self.helper.wait_for_dispatched_events(1)
        self.assertEqual(ack['event_type'], 'ack')
        self.assertEqual(ack['user_message_id'], message_id)
        self.assertEqual(ack['sent_message_id'], message_id)
