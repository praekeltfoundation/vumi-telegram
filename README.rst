Vumi Telegram Transport
=======================

.. image:: https://img.shields.io/travis/praekelt/vumi-telegram.svg
    :target: https://travis-ci.org/praekelt/vumi-telegram

.. image:: https://coveralls.io/repos/praekelt/vumi-telegram/badge.png?branch=develop
    :target: https://coveralls.io/r/praekelt/vumi-telegram?branch=develop
    :alt: Code Coverage


The Telegram transport allows any Vumi_ application to interact with Telegram users, by making use of the Telegram Bot API_.

Getting Started
===============

The best way to run the transport is by making use of Junebug_, which allows you to launch and manage Vumi transports using a RESTful HTTP interface. The transport also requires Redis and RabbitMQ to run. Install them as follows::

    $ sudo apt-get install redis-server rabbitmq-server
    $ pip install junebug
    $ pip install vxtelegram

You should have both Redis and RabbitMQ running to start the transport::

    $ sudo service redis-server start
    $ sudo service rabbitmq-server start

Launch Junebug with the Telegram channel configured::

    $ jb -p 8080 \
    $   --channels telegram:vxtelegram.telegram.TelegramTransport \
    $   --logging-path logs

.. note::

    If your logs end with something other than ``Got an authenticated AMQP connection``, you might have to change some RabbitMQ permissions. Run the following commands to set RabbitMQ up correctly::

        $ sudo rabbitmqctl add_user vumi vumi
        $ sudo rabbitmqctl add_vhost /develop
        $ sudo rabbitmqctl set_permissions -p /develop vumi '.*' '.*' '.*'

The Telegram transport sets up a webhook to receive updates from Telegram as they arise. This webhook has to be set up on an HTTPS URL, meaning that you'll need to run the transport behind a server or proxy capable of TLS (like ngrok_).

To create the channel and launch the transport, we can post the channel's config file to Junebug. The config should be in the following format (we'll call it ``config.json``):

.. code-block:: json

    {
        "type": "telegram",
        "amqp_queue": "messenger_transport",
        "config": {
            "web_path": "/telegram",
            "web_port": 8000,
            "transport_name": "telegram_transport",
            "outbound_url": "https://api.telegram.org/bot",
            "inbound_url": "YOUR_WEBHOOK_URL/telegram",
            "bot_username": "YOUR_BOT_USERNAME",
            "bot_token": "YOUR_BOT_TOKEN"
        }
    }

You should have received a bot token and username when you registered your bot with Telegram; if you haven't, do that now_. Your webhook URL is the URL of your server or proxy - that is, the URL you want Telegram to post updates to.

Post the config file to Junebug to launch the channel::

    $ curl -X POST -d@config.json http://localhost:8080/channels/

Your transport is now up and running! You can check which channels you have running in Junebug at any time by making the following request::

    $ curl -X GET http://localhost:8080/channels/

You can also view details of a specific channel by making a get request like::

    $ curl -X GET localhost:8080/channels/<channel_id>

and delete a channel by making a delete request to that same URL. Sending a message over your channel is as simple as::

    $ curl -X POST -d MESSAGE_PAYLOAD http://localhost:8080/channels/<channel_id>/messages/

Read here_ to see how your messages should be formatted.

Running the transport with a Vumi application
=============================================

Running a Vumi application as a Telegram bot is incredibly easy once the transport is running. For example, let's run a simple EchoWorker::

    $ twistd -n vumi_worker \
        --worker-class=vumi.demos.words.EchoWorker \
        --set-option=transport_name:telegram_transport

Rich message functionality
==========================

The Telegram transport is capable of sending messages with a variety of media attachments, as well as taking advantage of Telegram-specific functionality like inline queries and inline keyboards. These can be used by specifying the relevant payload in the ``helper_metadata`` of outbound messages.

Media attachments
~~~~~~~~~~~~~~~~~

Sending a media attachment is as simple as sending an ``attachment`` dict in the message's ``helper_metadata``. The media type should be one of 'photo', 'document', 'contact', 'location' or 'venue'.

See the Telegram API docs_ for more information, including what limitations apply and what parameters are available (note that some are required).

.. code-block:: python

    helper_metadata={
        'telegram': {
            'attachment': {
                'type': MEDIA_TYPE,
                 PARAM: VALUE,
            },
        },
    }

Inline queries
~~~~~~~~~~~~~~

When the transport receives an inline query, it publishes a message that contains the following ``helper_metadata``:

.. code-block:: python

    helper_metadata={
        'telegram': {
            'type': 'inline_query',
            'details': {'inline_query_id': INLINE_QUERY_ID},
            'telegram_username': TELEGRAM_USERNAME,
        },
    }

The ``content`` field contains the text of the inline query.

When answering inline queries, your application should reply directly to the message containing the query (as opposed to using the application's ``send_to`` method). A response to an inline query should have the following structure:

.. code-block:: python

    helper_metadata={
        'telegram': {
            'type': 'inline_query_reply',
            'results': [],
        },
    }

The ``results`` field should be an array of ``InlineQueryResult`` objects. See the Telegram inline mode documentation_ for more information.

Reply markup, formatting options and inline keyboards
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Telegram provides some optional parameters to the sendMessage_ method. These can simply be listed in ``helper_metadata`` as follows:

.. code-block:: python

    helper_metadata={
        'telegram': {
            PARAM: VALUE,
        },
    }

Using the parameter ``reply_markup``, it is possible to send a message with an inline keyboard embedded. When a button is pressed, a callback query is sent to our webhook. It is published like this:

.. code-block:: python

    helper_metadata={
        'telegram': {
            'type': 'callback_query',
            'details': {'callback_query_id': CALLBACK_QUERY_ID},
            'telegram_username': TELEGRAM_USERNAME,
        },
    }

The ``content`` field contains the callback data. You can respond to a callback query by formatting your ``helper_metadata`` as follows:

.. code-block:: python

    helper_metadata={
        'telegram': {
            'type': 'callback_query_reply',
            'details': {
                PARAM: VALUE,
            },
        },
    }

See the answerCallbackQuery_ documentation to find out what parameters you can use. Also note that when a user selects a button on an inline keyboard, a progress icon is displayed until ``answerCallbackQuery`` is called. Because of this, it is important to always respond to callback queries, even if you have nothing to send.

As with inline queries, make sure that your response is a direct reply to the original callback query message.

Things to note
==============

All inbound messages received by the transport are published with ``from_addr`` being the sender's Telegram user ID. However, since the ID is an simply integer, the transport will always include the user's username in ``helper_metadata``, under the field ``telegram_username``. This allows for more clarity in logs and statuses.

User profile information
~~~~~~~~~~~~~~~~~~~~~~~~

Telegram values privacy. Because of this, a user's profile contains nothing more than their user ID, username, and an optional profile picture and full name.

Retrieving this information is trivial if you have the user's ID and the ID of a chat they are participating in. The following request returns a user's profile information::

    $ curl -X GET -d '{
        "chat_id": CHAT_ID,
        "user_id": USER_ID
    }' https://api.telegram.org/bot<BOT_TOKEN>/getChatMember

You should receive a ``ChatMember`` object in the response body that looks like this, where STATUS is the user's role in the chat (for example 'administrator'):

.. code-block:: json

    {
        "user": {
            "id": "USER_ID",
            "first_name": "FIRST_NAME",
            "last_name": "LAST_NAME",
            "username": "USERNAME"
        },
        "status": "STATUS"
    }

Getting a user's profile photos is a little more complicated, using the following request::

    $ curl -X GET -d '{"user_id": USER_ID}' https://api.telegram.org/bot<BOT_TOKEN>/getUserProfilePhotos

This returns a ``UserProfilePhotos`` object that has the following structure:

.. code-block:: json

    {
        "total_count": "PHOTO_COUNT",
        "photos": [[
            {
                "file_id": "FILE_ID",
                "width": 1,
                "height": 1,
                "file_size": 1,
            }
        ]]
    }

Using the file ID of the photo you want to download, call the getFile method with this request::

    $ curl -X GET -d '{"file_id": FILE_ID}' https://api.telegram.org/bot<BOT_TOKEN>/getFile

This returns a ``File`` object, which looks like this:

.. code-block:: json

    {
        "file_id": "FILE_ID",
        "file_size": 1,
        "file_path": "FILE_PATH"
    }

The file can then be downloaded at ``https://api.telegram.org/bot<BOT_TOKEN>/<FILE_PATH>``.

.. _Vumi: http://vumi.readthedocs.org
.. _Junebug: http://junebug.readthedocs.org
.. _API: https://core.telegram.org/bots/api
.. _ngrok: http://ngrok.io
.. _now: https://core.telegram.org/bots#3-how-do-i-create-a-bot
.. _here: #rich-message-functionality
.. _docs: https://core.telegram.org/bots/api#available-methods
.. _documentation: https://core.telegram.org/bots/api#inline-mode
.. _answerCallbackQuery: https://core.telegram.org/bots/api#answercallbackquery
.. _sendMessage: https://core.telegram.org/bots/api#sendmessage
