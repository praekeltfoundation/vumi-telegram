import json

from treq.client import HTTPClient

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
from twisted.web.client import Agent
from twisted.web.resource import Resource
from twisted.web._newclient import ResponseFailed

from vumi.transports.httprpc.httprpc import HttpRpcTransport, HttpRpcResource
from vumi.config import ConfigText
from vumi import log


class TelegramTransportConfig(HttpRpcTransport.CONFIG_CLASS):
    bot_username = ConfigText(
        'The username of our Telegram bot',
        static=True,
        required=True,
    )
    bot_token = ConfigText(
        "Our bot's unique token to access the Telegram API",
        static=True,
        required=True,
    )
    outbound_url = ConfigText(
        'The URL our bot should make requests to',
        default='https://api.telegram.org/bot', static=True,
    )


class TelegramResource(HttpRpcResource):
    isLeaf = True

    def __init__(self, transport):
        self.transport = transport
        Resource.__init__(self)

    def render(self, request, request_id=None):
        if request_id is None:
            pass
            # TODO: generate a request id somehow
        self.transport.handle_raw_inbound_message(request_id, request)
        return ''


class TelegramTransport(HttpRpcTransport):
    """
    Telegram transport for Vumi
    """
    transport_type = 'telegram'
    transport_name = 'telegram_transport'

    _requests = {}

    CONFIG_CLASS = TelegramTransportConfig

    # TODO: ensure we are not receiving duplicate updates
    updates_received = []

    @classmethod
    def agent_factory(cls):
        """For swapping out the Agent for use in tests"""
        return Agent(reactor)

    @inlineCallbacks
    def setup_webhook(self):
        config = self.get_static_config()
        addr = self.web_resource.getHost()
        inbound_url = addr.host + str(addr.port) + self.web_path
        query_string = self.outbound_url + '/setWebhook?url=' + inbound_url

        http_client = HTTPClient(self.agent_factory())

        try:
            r = yield http_client.post(query_string)
            response = yield json.loads(r.content())
        except ResponseFailed:
            pass
            # It seems that if our request contains invalid params, Telegram
            # redirects to the Bot API page instead of sending a proper
            # response, which throws HTTPClient for a loop.
            # TODO: handle page redirect, as well as other possible exceptions

    @inlineCallbacks
    def setup_transport(self):
        yield super(TelegramTransport, self).setup_transport

        config = self.get_static_config()
        self.outbound_url = config.outbound_url + config.bot_token
        self.bot_username = config.bot_username
        self.web_path = config.web_path
        self.web_port = config.web_port
        self.rpc_resource = TelegramResource(self)

        self.web_resource = yield self.start_web_resources(
            [
                (self.rpc_resource, self.web_path),
            ],
            self.web_port)

        yield self.setup_webhook()

    @inlineCallbacks
    def handle_raw_inbound_message(self, message_id, request):
        content = json.loads(request.content.read())
        update_id = content['update_id']

        # Telegram updates can contain objects other than messages (ignore if
        # that is the case)
        if 'message' not in content:
            log.info('Inbound update does not contain a message')
            return

        message = self.translate_inbound_message(content['message'])

        log.info(
            'TelegramTransport receiving inbound message from %s to %s' % (
                message['from_addr'], message['to_addr']))

        # This throws off the tests for some reason
        # request.finish()

        yield self.publish_message(
            message_id=message_id,
            content=message['content'],
            to_addr=message['to_addr'],
            from_addr=message['from_addr'],
            transport_type=self.transport_type,
            transport_name=self.transport_name,
        )

    def translate_inbound_message(self, message):
        """
        Translates inbound Telegram message into Vumi's preferred format
        """
        # We are only interested in text messages for now
        if 'text' in message:
            content = message['text']
        else:
            log.info('Message is not a text message.')
            content = ''

        to_addr = self.bot_username

        # Sender field is empty if message is sent over a Telegram channel as
        # opposed to being directly sent to our bot (in which case the channel
        # id is what we want)
        if 'from' in message:
            from_addr = message['from']['id']
        else:
            from_addr = message['chat']['id']

        message = {
            'content': content,
            'to_addr': to_addr,
            'from_addr': from_addr,
        }

        return message

    @inlineCallbacks
    def handle_outbound_message(self, message):
        message_id = message['message_id']

        # TODO: handle direct replies
        params = {
            'chat_id': message['to_addr'],
            'text': message['content'],
            'reply_to_message_id': '',
        }
        url = self.outbound_url + '/sendMessage'
        http_client = HTTPClient(self.agent_factory())

        try:
            r = yield http_client.post(url, params=params)
            # TODO: handle possible errors where responses are not JSON objects
            content = yield r.content()
            response = json.loads(content)
        except ResponseFailed:
            # TODO: Handle page redirect in event of our request being denied
            # This is a temporary fix for now
            response = {'ok': False, 'description': 'Page redirect', }

        if response['ok']:
            yield self.publish_ack(
                user_message_id=message_id,
                sent_message_id=message_id,
            )
        else:
            yield self.publish_nack(message_id, 'Failed to send message: %s' %
                                    response['description'])

    @inlineCallbacks
    def teardown_transport(self):
        yield self.web_resource.loseConnection()
