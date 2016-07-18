[![Build Status](https://travis-ci.org/praekelt/vumi-telegram.svg?branch=develop)](https://travis-ci.org/praekelt/vumi-telegram)
[![Coverage Status](https://coveralls.io/repos/github/praekelt/vumi-telegram/badge.svg?branch=develop)](https://coveralls.io/github/praekelt/vumi-telegram?branch=develop)

# Vumi Telegram transport

A [Vumi](http://github.com/praekelt/vumi) transport for [Telegram](http://telegram.org) and [Junebug](https://github.com/praekelt/junebug).

## Helpful links

- [Telegram bot API](https://core.telegram.org/bots/api)
- [Vumi documentation](http://vumi.readthedocs.io/en/latest)
- [Vumi GitHub repository](https://github.com/praekelt/vumi)
- [Junebug documentation](http://junebug.readthedocs.io/en/latest)
- [Junebug GitHub repository](https://github.com/praekelt/junebug)

## Demo

The following tutorial demonstrates how to get the Vumi Telegram transport up and running. We assume you are using a Unix-like operating system, as well as some very basic competency with the command line and Python. We also assume you have already created a Telegram bot and received an API token from Telegram (follow the instructions [here](https://core.telegram.org/bots) if you haven't yet done so).

The transport receives inbound messages from Telegram via webhooks. However, Telegram only supports HTTPS URLs for webhook setup. In production, you would need to run the transport behind a load balancer or proxy server - in this tutorial, we will be using [ngrok](https://ngrok.com).

Download it from the [ngrok website](https://ngrok.com/download) and follow the installation instructions. To test that the install was successful, run the following command:

```
$ ngrok http 8000
```

You should see the ngrok command line interface displayed.

Now it's time to install the transport. First, navigate to your working directory. You should probably set up a virtual environment at this point - see the [virtualenv documentation](https://virtualenv.pypa.io/en/stable/) if you're not sure how. This isn't strictly necessary, but the rest of the tutorial will assume you're working inside a virtual environment. Ignore the `(ve)` prefixes if you aren't, or if you've named yours differently.

Once your virtual environment is set up, install the transport using pip with the following command:

```
(ve) $ pip install -e git+git://github.com/praekelt/vumi-telegram.git@develop#egg=vumi-telegram
```

This will install the development branch of the vumi-telegram repo. It is kept stable, but is not recommended for production.

Now that your transport is installed, it's time to configure it. Vumi transports use YAML configuration files - create a config file (we'll call ours "config.yml") in your working directory and fill in the following configurations:

```yaml
bot_username: "@my_bot"                       # Your bot's username
bot_token: "<my-bot-token>"                   # The API token you received from the Telegram @BotFather
transport_name: "telegram_transport"          # Your application worker will use this name to connect to your transport
web_path: "/telegram"                         # The web path your transport will listen on
web_port: 8000                                # The web port your transport will listen on
outbound_url: "https://api.telegram.org/bot"  # The Telegram API base URL
inbound_url: ""                               # We will set up our webhook on this URL (leave blank for now)
```

Time to start your ngrok tunnel. Run `ngrok http 8000` in a separate terminal window, and copy the "forwarding" URL. Then paste it into the `inbound_url` field in your config file in the following format:

```yaml
inbound_url: "https://<ngrok-forwarding-url>/telegram"    # The path should match the path specified in web_path
```

Your ngrok tunnel is now listening for requests, and routing them to localhost:8000, which is where our transport is configured to listen. We have also configured the transport to use the tunnel's URL as the `inbound_url` - this means it will set up the Telegram webhook on this URL. You can visit localhost:4040 in your browser to inspect requests as we receive them.

Now it's time to run your transport. First, make sure you have a Redis server running, as our transport caches recent updates using Redis to prevent duplicate updates being processed. Run the following command:

```
(ve) $ service redis-server start
```

You should also have a RabbitMQ server runnning, as our transport and application workers use RabbitMQ to relay messages to one another. Run the following command:

```
(ve) $ service rabbitmq-server start
```

> Note: you might have to run the above commands using `sudo`.

Now start your transport with the following command:

```
(ve) $ twistd -n --pidfile=transportworker.pid vumi_worker \
     $ --worker-class vxtelegram.telegram.TelegramTransport \
     $ --config=./config.yml
```

> Note: ensure you are using the correct name and file path for your config file.

You should see a whole lot of logs filling up your window. You'll know everything is working like it should be if the most recent log looks something like this:

```
<timestamp> [log_legacy#info] Webhook set up on <your-inbound_url-here>
```

We can test it out by running Vumi's built in EchoWorker application. This worker should receive messages we send to our bot, and simply echo them back to us. It requires zero setup, since it's already prepackaged with Vumi.

Start the EchoWorker by running the following command:

```
(ve) $ twistd -n --pidfile=applicationworker.pid vumi_worker \
     $ --worker-class vumi.demos.words.EchoWorker \
     $ --set-option=transport_name:telegram_transport
```

> Note: ensure your transport_name is the same name you gave your transport in its config file.

Now to try it out! Open a Telegram chat with your bot on your phone or in your browser (visit telegram.me/yourBotUsername) and send it some messages - you should have them echoed right back to you.

Let's try it with a slightly more complex setup. We'll use our own application worker, which we're going to write ourselves. It will generate seemingly intelligent replies to our messages, and is closely based on the second half of the official Vumi tutorial, which you can find [here](http://vumi.readthedocs.io/en/latest/intro/tutorial02.html).

First, we need to install PyAIML. Use the following command:

```
(ve) $ pip install PyAIML
```

Now we'll need a "brain" for our bot. For convenience, we'll be downloading an existing brain (her name is Alice, in case you're wondering). Use the following command:

```
(ve) $ wget https://github.com/downloads/praekelt/public-eggs/alice.brn
```

This is a good time to touch base. What have we done so far?

- We have an ngrok tunnel running, that routes all inbound requests to localhost:8000
- We have a Telegram transport running, that listens for inbound updates on localhost:8000
- We have a brain for our bot to use, to generate intelligent replies

All that remains is the application worker, which will receive messages from the transport, generate replies using our brain, and send them back to the user through the transport. We'll be writing this application ourselves. Create a Python script for your application - you can call it whatever you like, but we'll be calling ours "alice.py". Write the following Python code:

```python
import aiml
from vumi.application.base import ApplicationWorker

class AliceApplicationWorker(ApplicationWorker):
    """
    Our Alice application extends the base Vumi ApplicationWorker.
    """

    def __init__(self, *args, **kwargs):
        self.bot = aiml.Kernel()
        self.bot.bootstrap(brainFile='alice.brn')
        return super(AliceApplicationWorker, self).__init__(*args, **kwargs)

    def consume_user_message(self, message):
        """
        Receive messages from transport and generate replies.
        """
        message_content = message['content']
        message_user = message.user()
        response = self.bot.respond(message_content, message_user)
        self.reply_to(message, response)
```

> Did you notice that there is no mention of Telegram or the Telegram transport anywhere in the above code? One of the core Vumi philosophies is that applications should not care where messages come from or where they are sent to - all they care about is what they're meant to do with them. Likewise, transports do not not care what happens to the messages they relay. This means that you can use the same transport with any application, or the same application with any transport.
See the [Vumi documentation](http://vumi.readthedocs.io/en/latest) for more info.

It's time to run our application. Run the following command in a separate terminal window (make sure your ngrok server and transport are both still runnng):

```
(ve) $ twistd -n --pidfile=applicationworker.pid vumi_worker \
     $ --worker-class alice.AliceApplicationWorker \
     $ --set-option=transport_name:telegram_transport
```

> Note: again, make sure your transport_name is set correctly. Also, make sure that your worker class is pointing to the correct class (that is, your_python_script_here.AliceApplicationWorker).

Your application should be up and running! Open a chat with your bot and start a conversation. You should receive semi-intelligent replies.

The demo ends here. As you can imagine, this is a very basic set up. Because the transport is a separate entity to the application, there is no limit to what your application can do. Check out the Telegram API's [inline mode](https://core.telegram.org/bots/inline) documentation for some ideas as to how you can enrich your bot's user experience.
