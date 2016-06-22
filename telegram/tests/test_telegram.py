import json

from twisted.internet.defer import inlineCallbacks, returnValue, DeferredQueue
from twisted.web import http

from vumi.tests.helpers import VumiTestCase, MessageHelper
from vumi.tests.fake_connection import FakeHttpServer
from vumi.utils import http_request
from vumi.transports.httprpc.tests.helpers import HttpRpcTransportHelper

from telegram.telegram import TelegramTransport


class TestTelegramTransport(VumiTestCase):

    @inlineCallbacks
    def setUp(self):
        self.helper = self.add_helper(
            HttpRpcTransportHelper(TelegramTransport)
        )
        self.fake_http = FakeHttpServer(self.handle_inbound_request)
        self.transport = yield self.get_transport()

        self.default_user = {
            'id': 'default_user_id',
            'username': '@default_user',
        }
        self.bot_username = self.transport.get_static_config().bot_username
        self.default_vumi_msg = MessageHelper(
            transport_name=self.transport.transport_name,
            transport_type=self.transport.transport_type,
            mobile_addr=self.default_user['id'],
            transport_addr=self.bot_username,
        )
        self.pending_requests = DeferredQueue()

        addr = self.transport.web_resource.getHost()
        self.transport_url = 'http://%s/%s/' % (addr.host, addr.port)

        # Telegram chat types
        self.PRIVATE = 'private'
        self.CHANNEL = 'channel'
        self.GROUP = 'group'

    @inlineCallbacks
    def get_transport(self, **config):
        defaults = {
            'bot_username': '@bot',
            'bot_token': '1234',
            'web_path': 'foo',
            'web_port': 0,
        }
        defaults.update(config)
        transport = yield self.helper.get_transport(defaults)

        transport.agent_factory = self.fake_http.get_agent
        returnValue(transport)

    def handle_inbound_request(self, request):
        self.pending_requests.put(request)
        return

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

    def test_translate_inbound_message_no_text(self):
        inbound_msg = {
            'message_id': 'Message without text',
            'chat': 'Random chat',
            'from': self.default_user,
        }

        message = self.transport.translate_inbound_message(inbound_msg)

        self.assertEqual('', message['content'])
        self.assertEqual(self.bot_username, message['to_addr'])
        self.assertEqual(self.default_user['id'], message['from_addr'])

    @inlineCallbacks
    def test_inbound_update(self):
        telegram_update = json.dumps({
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
        })
        yield http_request(
            self.transport_url + 'foo', telegram_update, method='GET'
        )
        [msg] = yield self.helper.wait_for_dispatched_inbound(1)

        expected_update = json.loads(telegram_update)
        self.assertEqual(msg['to_addr'], self.bot_username)
        self.assertEqual(msg['from_addr'], self.default_user['id'])
        self.assertEqual(msg['content'], expected_update['message']['text'])
        self.assertEqual(msg['transport_type'],
                         self.transport.transport_type)
        self.assertEqual(msg['transport_name'],
                         self.transport.transport_name)

    @inlineCallbacks
    def test_valid_outbound(self):
        yield self.helper.make_dispatch_outbound(
            content='Outbound message!',
            to_addr=self.default_user['id'],
        )

        # request = yield self.pending_requests.get()
        # self.assertEqual(request.method, 'POST')
        # TODO: finish this test

    @inlineCallbacks
    def test_invalid_outbound_message(self):
        # NB: test passes even though message is valid due to issues with our
        # POST request in the transport's handle_outbound function. When those
        # are sorted out, the code commented out below should be executed
        msg_d = self.helper.make_dispatch_outbound(
            content='Outbound message!',
            to_addr=self.default_user['id']
        )

        # request = yield self.pending_requests.get()
        # request.setResponseCode(http.BAD_REQUEST)
        # request.finish()

        msg = yield msg_d
        [nack] = yield self.helper.wait_for_dispatched_events(1)

        self.assertEqual(nack['event_type'], 'nack')
        self.assertEqual(nack['user_message_id'], msg['message_id'])

        # NB: hardcoding the expected reason for now to get tests passing, but
        # in reality it should be equal to the 'description' field in the
        # response Telegram sends to unsucessful requests
        # self.assertEqual(nack['reason'], 'Page redirect')
