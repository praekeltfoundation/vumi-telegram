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

        # Telegram redirects our request if our token is in an invalid format
        if r.code == http.FOUND:
            self.log.warning('Webhook setup failed: request redirected')
            yield self.add_status_bad_webhook(
                status_type='request_redirected',
                message='Webhook setup failed: request redirected',
                details={
                    'error': 'Unexpected redirect',
                    'res_code': http.FOUND,
                },
            )
            return

        try:
            res = yield r.json()
        except ValueError as e:
            content = yield r.content()
            self.log.warning(
                'Webhook setup failed: unexpected response format'
            )
            yield self.add_status_bad_webhook(
                status_type='unexpected_response_format',
                message='Webhook setup failed: unexpected response format',
                details={
                    'error': e.message,
                    'res_code': r.code,
                    'res_body': content,
                },
            )
            return

        if r.code == http.OK and res['ok']:
            self.log.info('Webhook set up on %s' % self.inbound_url)
            yield self.add_status_good_webhook()
        else:
            self.log.warning('Webhook setup failed: %s' % res['description'])
            yield self.add_status_bad_webhook(
                status_type='bad_response',
                message='Webhook setup failed: bad response from Telegram',
                details={
                    'error': res['description'],
                    'res_code': r.code,
                }
            )

    def add_status_good_webhook(self):
        return self.add_status(
            status='ok',
            component='telegram_webhook',
            type='webhook_setup_success',
            message='Webhook setup successful',
            details={
                'webhook_url': self.inbound_url,
            },
        )

    def add_status_bad_webhook(self, status_type, message, details):
        return self.add_status(
            status='down',
            component='telegram_webhook',
            type=status_type,
            message=message,
            details=details,
        )

    def get_outbound_url(self, path):
        return '%s/%s' % (self.api_url, path)

    @inlineCallbacks
    def handle_raw_inbound_message(self, message_id, request):
        # TODO: ensure we are not receiving duplicate updates
        # TODO: support inline queries
        content = yield request.content.read()
        try:
            update = json.loads(content)
        except ValueError as e:
            self.log.warning('Inbound update in unexpected format: %s' % e)
            yield self.add_status_bad_inbound(
                status_type='unexpected_update_format',
                message='Inbound update in unexpected format',
                details={
                    'error': e.message,
                    'req_content': content,
                },
            )
            request.setResponseCode(http.BAD_REQUEST)
            request.finish()
            return

        # Handle inline queries separately to text messages
        if 'inline_query' in update:
            yield self.handle_inbound_inline_query(
                message_id=message_id,
                inline_query=update['inline_query'],
            )
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
            self.log.info('Inbound message is not a text message')
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

    def add_status_bad_inbound(self, status_type, message, details):
        return self.add_status(
            status='down',
            component='telegram_inbound',
            type=status_type,
            message=message,
            details=details,
        )

    @inlineCallbacks
    def handle_inbound_inline_query(self, message_id, inline_query):
        # NOTE: Telegram supports multiple ways to answer inline queries with
        #       rich content (articles, multimedia etc.) as opposed to simple
        #       text messages.
        #       see: https://core.telegram.org/bots/api#answerinlinequery
        self.log.info(
            'TelegramTransport receiving inline query from %s to %s' % (
                inline_query['from']['id'], self.bot_username))

        yield self.publish_message(
            message_id=message_id,
            content=None,
            to_addr=self.bot_username,
            from_addr=inline_query['from']['id'],
            transport_type=self.transport_type,
            transport_name=self.transport_name,
            transport_metadata={
                'message_type': 'inline_query',
                'details': {
                    'query': inline_query['query'],
                },
            },
        )

        yield self.add_status(
            status='ok',
            component='telegram_inbound',
            type='good_inbound',
            message='Good inbound request',
        )

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
                message='Message not sent: request redirected',
                status_type='request_redirected',
                details={
                    'error': 'Unexpected redirect',
                    'res_code': http.FOUND,
                },
            )
            return

        try:
            res = yield r.json()
        except ValueError as e:
            content = yield r.content()
            yield self.outbound_failure(
                message_id=message_id,
                message='Message not sent: unexpected response format',
                status_type='unexpected_response_format',
                details={
                    'error': e.message,
                    'res_code': r.code,
                    'res_body': content,
                },
            )
            return

        if r.code == http.OK and res['ok']:
            yield self.outbound_success(message_id)
        else:
            yield self.outbound_failure(
                message_id=message_id,
                message='Message not sent: bad response from Telegram',
                status_type='bad_response',
                details={
                    'error': res['description'],
                    'res_code': r.code,
                },
            )

    @inlineCallbacks
    def outbound_failure(self, status_type, message_id, message, details):
        yield self.publish_nack(message_id, message)
        yield self.add_status_bad_outbound(status_type, message, details)

    @inlineCallbacks
    def outbound_success(self, message_id):
        yield self.publish_ack(message_id, message_id)
        yield self.add_status_good_outbound()

    def add_status_bad_outbound(self, status_type, message, details):
        return self.add_status(
            status='down',
            component='telegram_outbound',
            type=status_type,
            message=message,
            details=details,
        )

    def add_status_good_outbound(self):
        return self.add_status(
            status='ok',
            component='telegram_outbound',
            type='good_outbound_request',
            message='Outbound request successful',
        )

    def add_status_starting(self):
        return self.add_status(
            status='down',
            component='telegram_setup',
            type='starting',
            message='Telegram transport starting...',
        )
