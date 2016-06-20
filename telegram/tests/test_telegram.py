import json

from twisted.internet.defer import inlineCallbacks, returnValue

from vumi.tests.helpers import VumiTestCase, MessageHelper
from vumi.tests.fake_connection import FakeHttpServer
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

        self.bot_username = self.transport.CONFIG_CLASS.bot_username
        self.default_vumi_msg = MessageHelper(
            transport_name=self.transport.transport_name,
            transport_type=self.transport.transport_type,
            mobile_addr=self.user,
            transport_addr=self.bot_username,
        )
        self.default_user = json.dumps({
            'id': 'Default user',
        })

    @inlineCallbacks
    def get_transport(self, **config):
        defaults = {
            'bot_username': '@bot',
            'bot_token': '1234',
            'base_url': 'www.example.com',
        }
        defaults.update(config)
        transport = yield self.helper.get_transport(defaults)

        transport.agent_factory = self.fake_http.get_agent
        returnValue(transport)

    def handle_inbound_request(self, request):
        # TODO: handle inbound requests
        pass

    def test_translate_inbound_message_from_channel(self):
        default_channel = json.dumps({
            'id': 'Default channel',
            'type': 'channel',
        })

        inbound_msg = json.dumps({
            'message_id': 'Message from Telegram channel',
            'chat': self.default_channel,
            'text': 'Hi from Telegram channel!',
        })

        message = self.transport.translate_inbound_message(inbound_msg)

        self.assertEqual(inbound_msg['text'], message['content'])
        self.assertEqual(self.bot_username, message['to_addr'])
        self.assertEqual(default_channel['id'], message['from_addr'])

    def test_translate_inbound_message_from_user(self):
        inbound_msg = json.dumps({
            'message_id': 'Message from Telegram user',
            'chat': 'Random chat',
            'text': 'Hi from Telegram user!',
            'from': self.default_user,
        })

        message = self.transport.translate_inbound_message(inbound_msg)

        self.assertEqual(inbound_msg['text'], message['content'])
        self.assertEqual(self.bot_username, message['to_addr'])
        self.assertEqual(self.default_user['id'], message['from_addr'])

    def test_translate_inbound_message_no_text(self):
        inbound_msg = json.dumps({
            'message_id': 'Message without text',
            'chat': 'Random chat',
            'from': self.default_user,
        })

        message = self.transport.translate_inbound_message(inbound_msg)

        self.assertEqual('', message['content'])
        self.assertEqual(self.bot_username, message['to_addr'])
        self.assertEqual(self.default_user['id'], message['from_addr'])
