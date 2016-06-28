import json

from treq.client import HTTPClient

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
from twisted.web import http
from twisted.web.client import Agent
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
    inbound_url = ConfigText(
        'The URL our transport will listen on for Telegram updates',
        static=True, required=True,
    )


class TelegramTransport(HttpRpcTransport):
    """
    Telegram transport for Vumi
    """
    transport_type = 'telegram'
    transport_name = 'telegram_transport'
    _requests = {}

    CONFIG_CLASS = TelegramTransportConfig

    @classmethod
    def agent_factory(cls):
        """For swapping out the Agent for use in tests"""
        return Agent(reactor)

    @inlineCallbacks
    def setup_webhook(self):
        # NOTE: Telegram currently only supports ports 80, 88, 443 and 8443 for
        #       webhook setup
        url = '%s/setWebhook' % self.outbound_url.rstrip('/')
        http_client = HTTPClient(self.agent_factory())

        try:
            r = yield http_client.post(
                url=url,
                data=json.dumps({'url': self.inbound_url}),
                headers={'Content-Type': ['application/json']}
                )
            content = yield r.content()
            res = json.loads(content)
            if r.code == http.OK and res['ok']:
                log.info('Webhook set up on %s' % self.inbound_url)
            else:
                log.info('Webhook setup failed: %s' % res['description'])

        # Treat page redirects as errors, since Telegram seems to redirect us
        # when our bot token is invalid
        except ResponseFailed:
            log.info('Webhook setup failed: Invalid token')
        # In case we get a response from Telegram that isn't JSON
        except ValueError:
            log.info('Webhook setup failed: Unexpected response')

    @inlineCallbacks
    def setup_transport(self):
        config = self.get_static_config()
        self.outbound_url = '%s%s' % (config.outbound_url.rstrip('/'),
                                      config.bot_token)
        self.inbound_url = config.inbound_url
        self.bot_username = config.bot_username
        self.web_path = config.web_path
        self.web_port = config.web_port
        self.clock = self.get_clock()
        self.rpc_resource = HttpRpcResource(self)

        self.web_resource = yield self.start_web_resources(
            [
                (self.rpc_resource, self.web_path),
            ],
            self.web_port)

        yield self.setup_webhook()

    def get_clock(self):
        return reactor

    @inlineCallbacks
    def handle_raw_inbound_message(self, message_id, request):
        update = json.load(request.content)
        # TODO: ensure we are not receiving duplicate updates

        # Telegram updates can contain objects other than messages (ignore if
        # that is the case)
        if 'message' not in update:
            log.info('Inbound update does not contain a message')
            request.finish()
            return
        else:
            message = update['message']

        if 'text' in message:
            message = self.translate_inbound_message(update['message'])
            log.info(
                'TelegramTransport receiving inbound message from %s to %s' % (
                    message['from_addr'], message['to_addr']))

            yield self.publish_message(
                message_id=message_id,
                content=message['content'],
                to_addr=message['to_addr'],
                from_addr=message['from_addr'],
                transport_type=self.transport_type,
                transport_name=self.transport_name,
            )
        else:
            log.info('Message is not a text message')

        request.finish()

    def translate_inbound_message(self, message):
        """
        Translates inbound Telegram message into Vumi's preferred format
        """
        content = message['text']
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
        # TODO: handle direct replies
        message_id = message['message_id']
        outbound_msg = {
            'chat_id': message['to_addr'],
            'text': message['content'],
        }
        url = '%s/sendMessage' % self.outbound_url.rstrip('/')
        http_client = HTTPClient(self.agent_factory())

        try:
            r = yield http_client.post(
                url=url,
                data=json.dumps(outbound_msg),
                headers={'Content-Type': ['application/json']},
            )
            content = yield r.content()
            res = json.loads(content)
            if r.code == http.OK and res['ok']:
                yield self.publish_ack(
                    user_message_id=message_id,
                    sent_message_id=message_id,
                )
            else:
                yield self.outbound_failure(message_id, res['description'])

        # Treat page redirects as errors, since Telegram seems to redirect us
        # when our bot token is invalid
        except ResponseFailed:
            yield self.outbound_failure(message_id, 'Invalid token')
        # In case we get a response from Telegram that isn't JSON
        except ValueError:
            yield self.outbound_failure(message_id, 'Unexpected response')

    @inlineCallbacks
    def outbound_failure(self, message_id, reason):
        yield self.publish_nack(message_id, 'Failed to send message: %s' %
                                reason)

    @inlineCallbacks
    def teardown_transport(self):
        yield self.web_resource.loseConnection()
