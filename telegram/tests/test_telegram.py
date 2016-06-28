import json

from twisted.internet.defer import inlineCallbacks, returnValue, DeferredQueue
from twisted.web.server import NOT_DONE_YET
from twisted.web import http

from vumi.tests.utils import LogCatcher
from vumi.tests.helpers import VumiTestCase
from vumi.tests.fake_connection import FakeHttpServer
from vumi.transports.httprpc.tests.helpers import HttpRpcTransportHelper

from telegram.telegram import TelegramTransport


class TestTelegramTransport(VumiTestCase):

    @inlineCallbacks
    def setUp(self):
        self.helper = self.add_helper(
            HttpRpcTransportHelper(TelegramTransport)
        )
        self.request_queue = DeferredQueue()
        self.pending_requests = []
        self.addCleanup(self.finish_requests)
        self.mock_server = FakeHttpServer(self.handle_inbound_request)

        self.default_user = {
            'id': 'default_user_id',
            'username': '@default_user',
        }
        self.bad_telegram_response = {
            'ok': False,
            'description': 'Bad request',
        }
        self.bot_username = '@bot'
        self.API_URL = 'https://api.telegram.org/bot'
        self.TOKEN = '1234'

        self.transport = yield self.get_transport()

        # Telegram chat types
        self.PRIVATE = 'private'
        self.CHANNEL = 'channel'
        self.GROUP = 'group'

    @inlineCallbacks
    def get_transport(self, **config):
        defaults = {
            'bot_username': self.bot_username,
            'bot_token': self.TOKEN,
            'web_path': 'foo',
            'web_port': 0,
            'inbound_url': 'www.example.com',
            'outbound_url': self.API_URL
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
    def test_setup_webhook_no_errors(self):
        d = self.transport.setup_webhook()
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

    @inlineCallbacks
    def test_setup_webhook_with_errors(self):
        d = self.transport.setup_webhook()
        req = yield self.get_next_request()

        req.setResponseCode(http.BAD_REQUEST)
        req.write(json.dumps(self.bad_telegram_response))
        with LogCatcher(message='Webhook') as lc:
            req.finish()
            yield d
            [log] = lc.messages()
            self.assertEqual(log, 'Webhook setup failed: Bad request')

    @inlineCallbacks
    def test_setup_webhook_with_invalid_token(self):
        d = self.transport.setup_webhook()
        req = yield self.get_next_request()

        req.setResponseCode(http.FOUND)
        req.redirect('www.redirected.com')

        with LogCatcher(message='Webhook') as lc:
            req.finish()
            yield d
            [logs] = lc.messages()
            log = logs.splitlines()[0]
            self.assertEqual(
                log,
                'Webhook setup failed: Invalid token (request redirected)'
                )

    @inlineCallbacks
    def test_setup_webhook_with_unexpected_response(self):
        d = self.transport.setup_webhook()
        req = yield self.get_next_request()

        req.setResponseCode(http.BAD_REQUEST)
        req.write("Wait, this isn't JSON...")
        with LogCatcher(message='Webhook') as lc:
            req.finish()
            yield d
            [logs] = lc.messages()
            log = logs.splitlines()[0]
            self.assertEqual(
                log,
                'Webhook setup failed: Unexpected response (expected JSON)'
                )

    def test_translate_inbound_message_from_channel(self):
        default_channel = {
            'id': 'Default channel',
            'type': 'channel',
        }
        inbound_msg = {
            'message_id': 'Message from Telegram channel',
            'chat': default_channel,
            'text': 'Hi from Telegram channel!',
        }

        message = self.transport.translate_inbound_message(inbound_msg)
        self.assertEqual(inbound_msg['text'], message['content'])
        self.assertEqual(self.bot_username, message['to_addr'])
        self.assertEqual(default_channel['id'], message['from_addr'])

    def test_translate_inbound_message_from_user(self):
        inbound_msg = {
            'message_id': 'Message from Telegram user',
            'chat': 'Random chat',
            'text': 'Hi from Telegram user!',
            'from': self.default_user,
        }

        message = self.transport.translate_inbound_message(inbound_msg)
        self.assertEqual(inbound_msg['text'], message['content'])
        self.assertEqual(self.bot_username, message['to_addr'])
        self.assertEqual(self.default_user['id'], message['from_addr'])

    @inlineCallbacks
    def test_inbound_update(self):
        update = {
            'update_id': 'update_id',
            'message': {
                'message_id': 'msg_id',
                'from': self.default_user,
                'chat': {
                    'id': 'chat_id',
                    'type': self.PRIVATE
                },
                'date': 1234,
                'text': 'Incoming message from Telegram!',
            }
        }

        with LogCatcher(message='TelegramTransport') as lc:
            res = yield self.helper.mk_request(_method='POST',
                                               _data=json.dumps(update))
            self.assertEqual(res.code, http.OK)
            [log] = lc.messages()
            self.assertEqual(
                log,
                'TelegramTransport receiving inbound message from %s to %s' %
                (self.default_user['id'], self.bot_username)
            )

        [msg] = yield self.helper.wait_for_dispatched_inbound(1)
        self.assertEqual(msg['to_addr'], self.bot_username)
        self.assertEqual(msg['from_addr'], self.default_user['id'])
        self.assertEqual(msg['content'], update['message']['text'])
        self.assertEqual(msg['transport_type'],
                         self.transport.transport_type)
        self.assertEqual(msg['transport_name'],
                         self.transport.transport_name)

    @inlineCallbacks
    def test_inbound_non_message_update(self):
        update = json.dumps({
            'update_id': 'update_id',
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
        update = json.dumps({
            'update_id': 'update_id',
            'message': {
                'message_id': 'msg_id',
                'object': 'This is not a text message...'
            }
        })
        d = self.helper.mk_request(_method='POST', _data=update)

        with LogCatcher(message='text') as lc:
            res = yield d
            [log] = lc.messages()
            self.assertEqual(log, 'Message is not a text message')
        self.assertEqual(res.code, http.OK)

    @inlineCallbacks
    def test_outbound_message_no_errors(self):
        expected_url = '%s%s/%s' % (self.API_URL.rstrip('/'), self.TOKEN,
                                    'sendMessage')
        msg = self.helper.make_outbound(
            content='Outbound message!',
            to_addr=self.default_user['id'],
            from_addr=self.bot_username,
        )
        d = self.helper.dispatch_outbound(msg)

        req = yield self.get_next_request()
        self.assertEqual(req.method, 'POST')
        self.assertEqual(req.path, expected_url)

        outbound_msg = json.load(req.content)
        self.assertEqual(outbound_msg['text'], 'Outbound message!')
        self.assertEqual(outbound_msg['chat_id'], self.default_user['id'])

        req.write(json.dumps({'ok': True}))
        req.finish()
        yield d

        [ack] = yield self.helper.wait_for_dispatched_events(1)
        self.assertEqual(ack['event_type'], 'ack')
        self.assertEqual(ack['user_message_id'], msg['message_id'])
        self.assertEqual(ack['sent_message_id'], msg['message_id'])

    @inlineCallbacks
    def test_outbound_message_with_errors(self):
        msg = yield self.helper.make_outbound(
            content='Outbound message!',
            to_addr=self.default_user['id']
        )
        d = self.helper.dispatch_outbound(msg)

        req = yield self.get_next_request()
        req.setResponseCode(http.BAD_REQUEST)
        req.write(json.dumps(self.bad_telegram_response))
        req.finish()
        yield d

        [nack] = yield self.helper.wait_for_dispatched_events(1)
        self.assertEqual(nack['event_type'], 'nack')
        self.assertEqual(nack['user_message_id'], msg['message_id'])
        self.assertEqual(nack['nack_reason'],
                         'Failed to send message: Bad request')

    @inlineCallbacks
    def test_outbound_message_with_unexpected_response(self):
        msg = yield self.helper.make_outbound(
            content='Outbound message!',
            to_addr=self.default_user['id']
            )
        d = self.helper.dispatch_outbound(msg)

        req = yield self.get_next_request()
        req.setResponseCode(http.BAD_REQUEST)
        req.write("Wait, this isn't JSON...")
        req.finish()
        yield d

        [nack] = yield self.helper.wait_for_dispatched_events(1)
        self.assertEqual(nack['event_type'], 'nack')
        self.assertEqual(nack['user_message_id'], msg['message_id'])
        self.assertEqual(
            nack['nack_reason'].splitlines()[0],
            'Failed to send message: Unexpected response (expected JSON)'
            )

    @inlineCallbacks
    def test_outbound_message_with_invalid_token(self):
        msg = yield self.helper.make_outbound(
            content='Outbound message!',
            to_addr=self.default_user['id'],
            )
        d = self.helper.dispatch_outbound(msg)

        req = yield self.get_next_request()
        req.setResponseCode(http.FOUND)
        req.redirect('www.redirected.com')
        req.finish()
        yield d

        [nack] = yield self.helper.wait_for_dispatched_events(1)
        self.assertEqual(nack['event_type'], 'nack')
        self.assertEqual(nack['user_message_id'], msg['message_id'])
        self.assertEqual(
            nack['nack_reason'].splitlines()[0],
            'Failed to send message: Invalid token (request redirected)'
            )
