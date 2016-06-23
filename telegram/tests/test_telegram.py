import json

from twisted.internet.defer import inlineCallbacks, returnValue, DeferredQueue
from twisted.web.server import NOT_DONE_YET

from vumi.tests.helpers import VumiTestCase, MessageHelper
from vumi.tests.fake_connection import FakeHttpServer
from vumi.tests.utils import LogCatcher
from vumi.transports.httprpc.tests.helpers import HttpRpcTransportHelper

from telegram.telegram import TelegramTransport


class TestTelegramTransport(VumiTestCase):

    @inlineCallbacks
    def setUp(self):
        self.helper = self.add_helper(
            HttpRpcTransportHelper(TelegramTransport)
        )
        self.pending_requests = DeferredQueue()
        self.mock_server = FakeHttpServer(self.handle_inbound_request)
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

        addr = self.transport.web_resource.getHost()
        self.transport_url = 'http://%s:%s/' % (addr.host, addr.port)

        # Telegram chat types
        self.PRIVATE = 'private'
        self.CHANNEL = 'channel'
        self.GROUP = 'group'

    @inlineCallbacks
    def get_transport(self, **config):
        defaults = {
            'bot_username': '@bot',
            'bot_token': '',
            'web_path': 'foo',
            'web_port': 0,
        }
        defaults.update(config)
        transport = yield self.helper.get_transport(defaults)
        transport.agent_factory = self.mock_server.get_agent
        returnValue(transport)

    def handle_inbound_request(self, request):
        self.pending_requests.put(request)
        return NOT_DONE_YET

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
        yield self.helper.mk_request(_method='POST', _data=telegram_update)
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
    def test_valid_outbound_message(self):
        msg = yield self.helper.make_dispatch_outbound(
            content='Outbound message!',
            to_addr=self.default_user['id'],
        )

        req = yield self.pending_requests.get()
        self.assertEqual(req.method, 'POST')

        outbound_msg = json.loads(req.content.read())
        self.assertEqual(outbound_msg['text'], 'Outbound message!')
        self.assertEqual(outbound_msg['chat_id'], self.default_user['id'])

        req.finish()

        [ack] = yield self.helper.wait_for_dispatched_events(1)

        self.assertEqual(ack['event_type'], 'ack')
        self.assertEqual(ack['user_message_id'], msg['message_id'])
        self.assertEqual(ack['sent_message_id'], msg['message_id'])

    @inlineCallbacks
    def test_invalid_outbound_message(self):
        msg = yield self.helper.make_dispatch_outbound(
            content='Outbound message!',
            to_addr=self.default_user['id']
        )

        req = yield self.pending_requests.get()
        req.write(
            json.dumps({'ok': False, 'description': 'Invalid request'})
        )
        req.finish()

        [nack] = yield self.helper.wait_for_dispatched_events(1)

        self.assertEqual(nack['event_type'], 'nack')
        self.assertEqual(nack['user_message_id'], msg['message_id'])
        self.assertEqual(nack['reason'], 'Invalid request')
