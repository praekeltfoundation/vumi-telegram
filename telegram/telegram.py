import json
import treq

from twisted.internet.defer import inlineCallbacks

from vumi.transports.httprpc import HttpRpcTransport
from vumi.config import ConfigText
from vumi import log


class TelegramTransportConfig(HttpRpcTransport.CONFIG_CLASS):
    bot_username = ConfigText(
        'The username of our Telegram bot',
        required=True
    )
    bot_token = ConfigText(
        "Our bot's unique token to access the Telegram API",
        required=True
    )


class TelegramTransport(HttpRpcTransport):
    """
    Telegram transport for Vumi
    """
    transport_type = 'telegram'
    transport_name = 'telegram_transport'

    CONFIG_CLASS = TelegramTransportConfig

    updates_received = []

    TOKEN = CONFIG_CLASS.bot_token
    API_URL = 'https://api.telegram.com/bot'

    @inlineCallbacks
    def setup_transport(self):
        URL = self.CONFIG_CLASS.web_path + str(self.CONFIG_CLASS.web_port)

        # Set up Webhook to receive Telegram updates for our bot
        r = yield treq.post(self.API_URL + self.TOKEN +
                            '/setWebhook?url=' + URL)
        response = json.loads(r.content)

        if not response['ok']:
            log.info('Error setting up Webhook: %s' % response['description'])
            # TODO: handle this error somehow

    @inlineCallbacks
    def handle_raw_inbound_message(self, message_id, request):
        request = json.loads(request)
        update_id = request['update_id']

        # Telegram updates can contain objects other than messages (ignore if
        # that is the case)
        if 'message' not in request:
            log.info('Inbound update does not contain a message')
            return

        message = self.translate_inbound_message(request['message'])

        log.info(
            'TelegramTransport receiving inbound message from %s to %s' % (
                message['from_addr'], message['to_addr']))

        # No need to keep request open
        self.finish_request(message_id, '')

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

        to_addr = self.CONFIG_CLASS.bot_username

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
        # Will need to be able to find Telegram message_id of original message
        params = {
            'chat_id': message['to_addr'],
            'text': message['content'],
            'reply_to_message_id': '',
        }
        r = yield treq.post(self.API_URL + self.TOKEN + '/sendMessage',
                            params=params)

        response = yield json.loads(r.content)

        if response['ok']:
            yield self.publish_ack(
                user_message_id=message_id,
                sent_message_id=message_id,
            )
        else:
            yield self.publish_nack(message_id, 'Failed to send message: %s' %
                                    response['description'])
