import json

from treq.client import HTTPClient

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
from twisted.web import http
from twisted.web.client import Agent

from vumi.transports.httprpc.httprpc import HttpRpcTransport
from vumi.config import ConfigText, ConfigUrl


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
    outbound_url = ConfigUrl(
        'The URL our bot should make requests to',
        default='https://api.telegram.org/bot', static=True,
    )
    inbound_url = ConfigUrl(
        'The URL our transport will listen on for Telegram updates',
        static=True, required=True,
    )


class TelegramTransport(HttpRpcTransport):
    """
    Telegram transport for Vumi
    """
    transport_type = 'telegram'
    transport_name = 'telegram_transport'

    CONFIG_CLASS = TelegramTransportConfig

    @classmethod
    def agent_factory(cls):
        """
        For swapping out the Agent for use in tests
        """
        return Agent(reactor)

    @inlineCallbacks
    def setup_transport(self):
        yield super(TelegramTransport, self).setup_transport()
        yield self.add_status_starting()

        config = self.get_static_config()
        self.api_url = '%s%s' % (config.outbound_url.geturl().rstrip('/'),
                                 config.bot_token)
        self.inbound_url = config.inbound_url.geturl()
        self.bot_username = config.bot_username

        yield self.setup_webhook()

    @inlineCallbacks
    def setup_webhook(self):
        # NOTE: Telegram currently only supports ports 80, 88, 443 and 8443 for
        #       webhook setup, and sends requests over HTTPS only
        url = self.get_outbound_url('setWebhook')
        http_client = HTTPClient(self.agent_factory())
        r = yield http_client.post(
            url=url,
            data=json.dumps({'url': self.inbound_url}),
            headers={'Content-Type': ['application/json']},
            allow_redirects=False,
            )

        # Telegram redirects our request if our bot token is invalid
        if r.code == http.FOUND:
            self.log.warning(
                'Webhook setup failed: Invalid bot token (redirected)')
            yield self.add_status_bad_webhook(
                'Invalid bot token (redirected)')
            return

        try:
            res = yield r.json()
        except ValueError as e:
            self.log.warning(
                'Webhook setup failed: Expected JSON response (code: %s)' % (
                    r.code))
            yield self.add_status_bad_webhook('Expected JSON response',
                                              error=r.code)
            return

        if r.code == http.OK and res['ok']:
            self.log.info('Webhook set up on %s' % self.inbound_url)
            yield self.add_status_good_webhook()
        else:
            self.log.warning('Webhook setup failed: %s' % res['description'])
            yield self.add_status_bad_webhook('Bad response from Telegram',
                                              error=res['description'])

    def get_outbound_url(self, path):
        return '%s/%s' % (self.api_url, path)

    @inlineCallbacks
    def handle_raw_inbound_message(self, message_id, request):
        # TODO: ensure we are not receiving duplicate updates
        # TODO: support inline queries?
        try:
            update = json.load(request.content)
        except ValueError as e:
            self.log.warning('Inbound update in unexpected format\n%s' % e)
            yield self.add_status_bad_inbound(
                message='Inbound update in unexpected format',
                error=e.message,
            )
            request.setResponseCode(http.BAD_REQUEST)
            request.finish()
            return

        # Ignore updates that do not contain message objects
        if 'message' not in update:
            self.log.info('Inbound update does not contain a message')
            request.finish()
            return

        # Ignore messages that aren't text messages
        message = update['message']
        if 'text' not in message:
            self.log.info('Message is not a text message')
            request.finish()
            return

        yield self.add_status(
            status='ok',
            component='telegram_inbound',
            type='good_inbound',
            message='Good inbound request',
        )

        message = self.translate_inbound_message(update['message'])
        self.log.info(
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
        request.finish()

    def translate_inbound_message(self, message):
        """
        Translates inbound Telegram message into Vumi's preferred format
        """
        content = message['text']
        to_addr = self.bot_username

        # Messages sent over channels do not contain a 'from' field - in that
        # case, we want the channel's chat id
        if 'from' in message:
            from_addr = message['from']['id']
        else:
            from_addr = message['chat']['id']

        return {
            'content': content,
            'to_addr': to_addr,
            'from_addr': from_addr,
        }

    @inlineCallbacks
    def handle_outbound_message(self, message):
        # TODO: handle direct replies
        message_id = message['message_id']
        outbound_msg = {
            'chat_id': message['to_addr'],
            'text': message['content'],
        }
        url = self.get_outbound_url('sendMessage')
        http_client = HTTPClient(self.agent_factory())

        r = yield http_client.post(
            url=url,
            data=json.dumps(outbound_msg),
            headers={'Content-Type': ['application/json']},
            allow_redirects=False,
        )

        # Telegram redirects our request if our bot token is invalid
        if r.code == http.FOUND:
            yield self.outbound_failure(
                message_id=message_id,
                message='Invalid bot token (redirected)',
                error='Redirected',
            )
            return

        try:
            res = yield r.json()
        except ValueError as e:
            yield self.outbound_failure(
                message_id=message_id,
                message='Expected JSON response',
                error='error code %s' % r.code,
            )
            return

        if r.code == http.OK and res['ok']:
            yield self.outbound_success(message_id)
        else:
            yield self.outbound_failure(
                message_id=message_id,
                message='Bad response from Telegram',
                error=res['description'],
            )

    @inlineCallbacks
    def outbound_failure(self, message_id, message, error=None):
        if error is None:
            error = ''
        yield self.publish_nack(message_id, '%s: %s' % (message, error))
        yield self.add_status_bad_outbound(message, error)

    @inlineCallbacks
    def outbound_success(self, message_id):
        yield self.publish_ack(message_id, message_id)
        yield self.add_status_good_outbound()

    def add_status_bad_inbound(self, message, error=None):
        details = {}
        if error is not None:
            details.update({'error': error})
        return self.add_status(
            status='down',
            component='telegram_inbound',
            type='bad_inbound',
            message=message,
            details=details,
        )

    def add_status_bad_outbound(self, message, error=None):
        details = {}
        if error is not None:
            details.update({'error': error})
        return self.add_status(
            status='down',
            component='telegram_outbound',
            type='bad_outbound',
            message=message,
            details=details,
        )

    def add_status_good_outbound(self):
        return self.add_status(
            status='ok',
            component='telegram_outbound',
            type='good_outbound',
            message='Outbound request successful',
        )

    def add_status_starting(self):
        return self.add_status(
            status='down',
            component='telegram_setup',
            type='starting',
            message='Telegram transport starting...',
        )

    def add_status_good_webhook(self):
        return self.add_status(
            status='ok',
            component='telegram_webhook',
            type='good_webhook',
            message='Webhook setup successful',
        )

    def add_status_bad_webhook(self, message, error=None):
        details = {}
        if error is not None:
            details.update({'error': error})
        return self.add_status(
            status='down',
            component='telegram_webhook',
            type='bad_webhook',
            message=message,
            details=details,
        )
