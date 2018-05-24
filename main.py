#!/usr/env/python3
#
# This file is part of YeenerBot, licensed under MIT
#
# Copyright (c) 2018 Emily Lau
# Copyright (c) 2018 Sylvia van Os
#
# See LICENSE for more information

import datetime
import io
import json
import logging
import os
import time
import random

from collections import OrderedDict
from distutils.util import strtobool

import dataset
import requests

from telegram import ParseMode, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Unauthorized, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, Filters, MessageHandler, Updater

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

def ensure_creator(function):
    def wrapper(bot, update, **optional_args):
        member = update.message.chat.get_member(update.message.from_user.id)
        if member.status != 'creator':
            bot.send_message(chat_id=update.message.chat_id, text="You do not have the required permission to do this.")
            return

        return function(bot=bot, update=update, **optional_args)

    return wrapper


def ensure_admin(function):
    def wrapper(bot, update, **optional_args):
        member = update.message.chat.get_member(update.message.from_user.id)
        if member.status not in ['creator', 'administrator']:
            bot.send_message(chat_id=update.message.chat_id, text="You do not have the required permission to do this.")
            return

        return function(bot=bot, update=update, **optional_args)

    return wrapper

class dict_no_keyerror(dict):
    def __missing__(self, key):
        return key

class DB():
    __db = dataset.connect('sqlite:///data.db')
    __group_table = __db['group']

    @staticmethod
    def get_group(group_id):
        group_data = DB().__group_table.find_one(group_id=group_id)
        if not group_data:
            return Group(group_id)

        filtered_group_data = {_key: group_data[_key] for _key in Group.get_keys() if _key in group_data}
        return Group(**filtered_group_data)

    @staticmethod
    def update_group(group):
        DB().__group_table.upsert(group.serialize(), ['group_id'])


class Group():
    def __init__(self, group_id, welcome_enabled=True, welcome_message=None, description=None, rules=None, relatedchats=None, bullet=None, chamber=None, warned=None):
        self.group_id = group_id
        self.welcome_enabled = welcome_enabled
        self.welcome_message = welcome_message
        self.description = description
        self.rules = rules
        self.relatedchats = relatedchats
        self.bullet = bullet if bullet is not None else random.randint(0,5)
        self.chamber = chamber if chamber is not None else 5
        self.warned = warned if warned is not None else json.dumps({})

    @staticmethod
    def get_keys():
        return ['group_id', 'welcome_enabled', 'welcome_message', 'description', 'rules', 'relatedchats', 'bullet', 'chamber', 'warned']

    def serialize(self):
        return {_key: getattr(self, _key) for _key in Group.get_keys()}

    def save(self):
        DB.update_group(self)


class ErrorHandler():
    def __init__(self, dispatcher):
        dispatcher.add_error_handler(self.handle_error)

    def handle_error(self, bot, update, error):
        if not update:
            return

        if type(error) == Unauthorized:
            text = "{}, I don't have permission to PM you. Please click the following link and then press START: {}.".format(update.message.from_user.name, 'https://telegram.me/{}?start=rules_{}'.format(bot.name[1:], update.message.chat.id))
            bot.send_message(chat_id=update.message.chat.id, text=text)
        else:
            text = "Oh no, something went wrong in {}!\n\nError message: {}".format(update.message.chat.title, error)
            bot.send_message(chat_id=Helpers.get_creator(update.message.chat).id, text=text)


class Helpers():
    @staticmethod
    def get_creator(chat):
        for admin in chat.get_administrators():
            if admin.status == "creator":
                return admin.user

    @staticmethod
    def list_mods(chat):
        creator = None
        mods = []
        for admin in chat.get_administrators():
            # Skip bots
            if admin.user.is_bot:
                continue

            if admin.status == "creator":
                creator = admin.user.name
            else:
                mods.append(admin.user.name)

        mods.sort()
        if creator:
            mods = ["{} (owner)".format(creator)] + mods

        return mods


    @staticmethod
    def get_description(bot, chat, group):
        return group.description if group.description else bot.get_chat(chat.id).description

    @staticmethod
    def get_invite_link(bot, chat):
        chat = bot.get_chat(chat.id)
        if not chat.invite_link:
            chat.invite_link = bot.export_chat_invite_link(chat.id)

        return chat.invite_link


class DebugHandler():
    def __init__(self, dispatcher):
        ping_handler = CommandHandler('ping', DebugHandler.ping)
        dispatcher.add_handler(ping_handler)

    @staticmethod
    def ping(bot, update):
        bot.send_message(chat_id=update.message.chat_id, parse_mode="html", text="<code>• {}</code>".format(random.choices([
            "Pong.",
            "Ha! I win.",
            "Damn, I missed!"
        ], weights=[90,5,5])[0]))


class GreetingHandler():
    def __init__(self, dispatcher):
        start_handler = CommandHandler('start', GreetingHandler.start)
        welcome_handler = MessageHandler(Filters.status_update.new_chat_members, GreetingHandler.welcome)
        setwelcome_handler = CommandHandler('setwelcome', GreetingHandler.set_welcome)
        togglewelcome_handler = CommandHandler('togglewelcome', GreetingHandler.toggle_welcome)
        dispatcher.add_handler(start_handler)
        dispatcher.add_handler(welcome_handler)
        dispatcher.add_handler(setwelcome_handler)
        dispatcher.add_handler(togglewelcome_handler)

    @staticmethod
    def start(bot, update):
        try:
            payload = update.message.text.split(' ', 1)[1]
        except IndexError:
            return

        if payload.startswith('rules_'):
            chat_id = payload[len('rules_'):]
            chat = bot.get_chat(chat_id)
            # Could be cleaner
            update.message.chat = chat
            RuleHandler.send_rules(bot, update)

    @staticmethod
    @ensure_admin
    def set_welcome(bot, update):
        group = DB().get_group(update.message.chat.id)
        text = "Welcome message set."
        try:
            group.welcome_message = update.message.text.split(' ', 1)[1]
        except IndexError:
            group.welcome_message = None
            text = "Welcome message reset to default."

        group.save()

        bot.send_message(chat_id=update.message.chat_id, text=text)

    @staticmethod
    @ensure_admin
    def toggle_welcome(bot, update):
        group = DB().get_group(update.message.chat.id)

        try:
            enabled = bool(strtobool(update.message.text.split(' ', 1)[1]))
        except (IndexError, ValueError):
            bot.send_message(chat_id=update.message.chat_id, text="Current status: {}. Please specify true or false to change.".format(group.welcome_enabled))
            return

        group.welcome_enabled = enabled
        group.save()

        bot.send_message(chat_id=update.message.chat_id, text="Welcome: {}".format(str(enabled)))

    @staticmethod
    def welcome(bot, update):
        group = DB().get_group(update.message.chat.id)
        if not group.welcome_enabled:
            return

        # Don't welcome bots (or ourselves)
        members = [member.name for member in update.message.new_chat_members if not member.is_bot]
        if len(members) == 0:
            return

        try:
            invite_link = Helpers.get_invite_link(bot, update.message.chat)
        except TelegramError:
            invite_link = None

        data = dict_no_keyerror({'usernames': ", ".join(members),
                                 'title': update.message.chat.title,
                                 'invite_link': invite_link,
                                 'mods': ", ".join(Helpers.list_mods(update.message.chat)),
                                 'description': Helpers.get_description(bot, update.message.chat, group),
                                 'rules_with_start': 'https://telegram.me/{}?start=rules_{}'.format(bot.name[1:], update.message.chat.id)})

        text = group.welcome_message if group.welcome_message else "Hello {usernames}, welcome to {title}! Please make sure to read the /rules by pressing the button below."

        bot.send_message(chat_id=update.message.chat_id,
                         text=text.format(**data),
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Click and press START to read the rules', url=data['rules_with_start'])]]))


class GroupInfoHandler():
    def __init__(self, dispatcher):
        description_handler = CommandHandler('description', GroupInfoHandler.description)
        setdescription_handler = CommandHandler('setdescription', GroupInfoHandler.set_description)
        relatedchats_handler = CommandHandler('relatedchats', GroupInfoHandler.relatedchats)
        setrelatedchats_handler = CommandHandler('setrelatedchats', GroupInfoHandler.set_relatedchats)
        invitelink_handler = CommandHandler('invitelink', GroupInfoHandler.invitelink)
        revokeinvitelink_handler = CommandHandler('revokeinvitelink', GroupInfoHandler.revokeinvitelink)
        dispatcher.add_handler(description_handler)
        dispatcher.add_handler(setdescription_handler)
        dispatcher.add_handler(relatedchats_handler)
        dispatcher.add_handler(setrelatedchats_handler)
        dispatcher.add_handler(invitelink_handler)
        dispatcher.add_handler(revokeinvitelink_handler)

    @staticmethod
    def relatedchats(bot, update):
        group = DB().get_group(update.message.chat.id)
        if group.relatedchats:
            bot.send_message(chat_id=update.message.from_user.id, text = "{}\n\nRelated chats:\n{}".format(update.message.chat.title, group.relatedchats))
        else:
            bot.send_message(chat_id=update.message.chat.id, text="There are no known related chats for this group")

    @staticmethod
    @ensure_admin
    def set_relatedchats(bot, update):
        group = DB().get_group(update.message.chat.id)
        text = "Related chats set."
        try:
            group.relatedchats = update.message.text.split(' ', 1)[1]
        except IndexError:
            group.relatedchats = None
            text = "Related chats cleared."

        group.save()

        bot.send_message(chat_id=update.message.chat_id, text=text)

    @staticmethod
    def description(bot, update):
        group = DB().get_group(update.message.chat.id)
        bot.send_message(chat_id=update.message.from_user.id, text = "{}\n\n{}".format(update.message.chat.title, Helpers.get_description(bot, update.message.chat, group)))

    @staticmethod
    @ensure_creator
    def set_description(bot, update):
        group = DB().get_group(update.message.chat.id)
        text = "Description set."
        try:
            group.description = update.message.text.split(' ', 1)[1]
        except IndexError:
            group.description = None
            text = "Description reset to default (fallback to Telegram description)."

        group.save()

        bot.send_message(chat_id=update.message.chat_id, text=text)

    @staticmethod
    def invitelink(bot, update):
        invite_link = Helpers.get_invite_link(bot, update.message.chat)
        if not invite_link:
            bot.send_message(chat_id=update.message.chat.id, text="{} does not have an invite link".format(update.message.chat.title))
            return

        bot.send_message(chat_id=update.message.chat.id, text="Invite link for {} is {}".format(update.message.chat.title, invite_link))

    @staticmethod
    @ensure_creator
    def revokeinvitelink(bot, update):
        bot.export_chat_invite_link(update.message.chat.id)
        bot.send_message(chat_id=update.message.chat.id, text="Invite link for {} revoked".format(update.message.chat.title))


class RandomHandler():
    def __init__(self, dispatcher):
        roll_handler = CommandHandler('roll', RandomHandler.roll)
        flip_handler = CommandHandler('flip', RandomHandler.flip)
        shake_handler = CommandHandler('shake', RandomHandler.shake)
        roulette_handler = CommandHandler('roulette', RandomHandler.roulette)
        dispatcher.add_handler(roll_handler)
        dispatcher.add_handler(flip_handler)
        dispatcher.add_handler(shake_handler)
        dispatcher.add_handler(roulette_handler)

    @staticmethod
    def roll(bot, update):
        try:
            roll = update.message.text.split(' ', 2)[1]
            dice = [int(n) for n in roll.split('d', 1)]
        except (IndexError, ValueError):
            dice = [1, 20]

        if dice[0] < 1 or dice[1] < 1:
            bot.send_message(chat_id=update.message.chat_id, text="Very funny.")
            return

        if dice[0] >= 1000 or dice[1] >= 1000:
            bot.send_message(chat_id=update.message.chat_id, text="Sorry, but I'm limited to 999d999.")
            return

        if dice[0] == 1:
            bot.send_message(chat_id=update.message.chat_id, text=str(random.randint(1, dice[1])))
            return

        results = []
        for i in range(0, dice[0]):
            results.append(random.randint(1, dice[1]))

        bot.send_message(chat_id=update.message.chat_id, text="{} = {}".format(" + ".join([str(result) for result in results]), str(sum(results))))

    @staticmethod
    def flip(bot, update):
        bot.send_message(chat_id=update.message.chat_id, parse_mode="html", text="<code>• {}</code>".format(random.choices([
            "Heads.",
            "Tails.",
            "The coin has landed sideways."
        ], weights=[45,45,10])[0]))

    @staticmethod
    def shake(bot, update):
        bot.send_message(chat_id=update.message.chat_id, parse_mode="html", text="<code>• {}</code>".format(random.choice([
            "It is certain.",
            "It is decidedly so.",
            "Without a doubt.",
            "Yes, definitely.",
            "You may rely on it.",
            "As I see it, yes.",
            "Most likely.",
            "Outlook good.",
            "Yes.",
            "Signs point to yes.",
            "Reply hazy, try again.",
            "Ask again later.",
            "Better not tell you now.",
            "Cannot predict now.",
            "Concentrate and ask again.",
            "Don't count on it.",
            "My reply is no.",
            "My sources say no.",
            "Outlook not so good.",
            "Very doubtful."
        ])))

    @staticmethod
    def roulette(bot, update):
        group = DB().get_group(update.message.chat.id)

        # Go to next chamber
        if group.chamber == 5:
            group.chamber = 0
        else:
            group.chamber += 1
        group.save()

        # Check if bullet is in chamber
        if group.bullet == group.chamber:
            bot.send_message(chat_id=update.message.chat_id, parse_mode="html", text="<code>• *BOOM!* Your brain is now all over the wall behind you.</code>")
            group.bullet = random.randint(0,5)
            group.chamber = 5
            group.save()
            for admin in update.message.chat.get_administrators():
                if admin.user.id == update.message.from_user.id:
                    return
            bot.send_message(chat_id=update.message.from_user.id, text = Helpers.get_invite_link(bot, update.message.chat))
            bot.kick_chat_member(chat_id=update.message.chat_id, user_id=update.message.from_user.id)
            bot.unban_chat_member(chat_id=update.message.chat_id, user_id=update.message.from_user.id)
        else:
            chambersremaining = 5 - group.chamber
            bot.send_message(chat_id=update.message.chat_id, parse_mode="html", text="<code>• *Click* You're safe. For now.\n{} chamber{} remaining.</code>".format(chambersremaining,"s" if chambersremaining != 1 else ""))


class RuleHandler():
    def __init__(self, dispatcher):
        rules_handler = CommandHandler('rules', RuleHandler.send_rules)
        setrules_handler = CommandHandler('setrules', RuleHandler.set_rules)
        dispatcher.add_handler(rules_handler)
        dispatcher.add_handler(setrules_handler)

    @staticmethod
    @ensure_admin
    def set_rules(bot, update):
        group = DB().get_group(update.message.chat.id)
        text = "Rules set."
        try:
            group.rules = update.message.text.split(' ', 1)[1]
        except IndexError:
            group.rules = None
            text = "Rules removed."

        group.save()

        bot.send_message(chat_id=update.message.chat_id, text=text)

    @staticmethod
    def send_rules(bot, update):
        # Notify owner
        try:
            bot.send_message(chat_id=Helpers.get_creator(update.message.chat).id, text="{} just requested the rules for {}.".format(update.message.from_user.name, update.message.chat.title))
        except Unauthorized:
            pass

        group = DB().get_group(update.message.chat.id)

        if not group.rules:
            bot.send_message(chat_id=update.message.chat.id, text="No rules set for this group yet. Just don't be a meanie, okay?")
            return

        text = "{}\n\n".format(update.message.chat.title)
        description = Helpers.get_description(bot, update.message.chat, group)
        if description:
            text += "{}\n\n".format(description)

        text += "The group rules are:\n{}\n\n".format(group.rules)
        text += "Your mods are:\n{}".format("\n".join(Helpers.list_mods(update.message.chat)))

        if group.relatedchats:
            text += "\n\nRelated chats:\n{}".format(group.relatedchats)

        bot.send_message(chat_id=update.message.from_user.id, text=text)

class ModerationHandler():
    def __init__(self, dispatcher):
        warn_handler = CommandHandler('warn', ModerationHandler.warn)
        kick_handler = CommandHandler('kick', ModerationHandler.kick)
        dispatcher.add_handler(warn_handler)
        dispatcher.add_handler(kick_handler)

    @staticmethod
    @ensure_admin
    def warn(bot, update):
        if not update.message.reply_to_message:
            bot.send_message(chat_id=update.message.chat.id, text="Reply to a message to warn the person who wrote it.")
            return

        group = DB().get_group(update.message.chat.id)
        warnings = json.loads(group.warned)
        message = update.message.reply_to_message
        if str(message.from_user.id) not in warnings:
            warnings[str(message.from_user.id)] = []

        try:
            reason = update.message.text.split(' ', 1)[1]
        except IndexError:
            reason = None

        warnings[str(message.from_user.id)].append({'timestamp': time.time(), 'reason': reason, 'warnedby': update.message.from_user.id})
        group.warned = json.dumps(warnings)
        group.save()

        warningtext = "{}, you just received a warning. Here are all warnings since you joined:\n".format(message.from_user.name)
        for warning in reversed(warnings[str(message.from_user.id)]):
            try:
                warnedby = update.message.chat.get_member(warning['warnedby'])
            except TelegramError:
                # If we can't find the warner in the chat anymore, assume they're no longer a mod and the warning is invalid.
                continue

            warningtext += "\n[{}] warned by {} (reason: {})".format(str(datetime.datetime.fromtimestamp(warning['timestamp'])).split(".")[0], warnedby.user.name, warning['reason'] if warning['reason'] else "none given")

        bot.send_message(chat_id=update.message.chat.id, text=warningtext)

    @staticmethod
    @ensure_admin
    def kick(bot, update):
        if not update.message.reply_to_message:
            bot.send_message(chat_id=update.message.chat.id, text="Reply to a message to kick the person who wrote it.")
            return

        message = update.message.reply_to_message
        bot.kick_chat_member(chat_id=message.chat_id, user_id=message.from_user.id)

class SauceNaoHandler():
    def __init__(self, dispatcher):
        saucenao_handler = CommandHandler('source', SauceNaoHandler.get_source)
        dispatcher.add_handler(saucenao_handler)

    @staticmethod
    def get_source(bot, update):
        if not update.message.reply_to_message:
            bot.send_message(chat_id=update.message.chat.id, text="You didn't reply to the message you want the source of.")
            return

        message = update.message.reply_to_message
        if len(message.photo) == 0:
            bot.send_message(chat_id=update.message.chat.id, text="I see no picture here.")
            return

        picture = bot.get_file(file_id=message.photo[0].file_id)
        picture_data = io.BytesIO()
        picture.download(out=picture_data)
        request_url = 'https://saucenao.com/search.php?output_type=2&numres=1&api_key={}'.format(saucenao_token)
        r = requests.post(request_url, files={'file': ("image.png", picture_data.getvalue())})
        if r.status_code != 200:
            bot.send_message(chat_id=update.message.chat.id, text="SauceNao failed me :( HTTP {}".format(r.status_code))
            return

        result_data = json.JSONDecoder(object_pairs_hook=OrderedDict).decode(r.text)
        if int(result_data['header']['results_returned']) == 0:
            bot.send_message(chat_id=update.message.chat.id, text="Couldn't find a source :(")
            return

        results = sorted(result_data['results'], key=lambda result: float(result['header']['similarity']))

        bot.send_message(chat_id=update.message.chat.id, text="I'm {}% sure this is the source: {}".format(results[-1]['header']['similarity'], results[-1]['data']['ext_urls'][0]))


# Setup
token = os.environ['TELEGRAM_BOT_TOKEN']
saucenao_token = os.environ['SAUCENAO_TOKEN']
updater = Updater(token=token)
dispatcher = updater.dispatcher

# Initialize handler
ErrorHandler(dispatcher)
DebugHandler(dispatcher)
GreetingHandler(dispatcher)
GroupInfoHandler(dispatcher)
RandomHandler(dispatcher)
RuleHandler(dispatcher)
ModerationHandler(dispatcher)
SauceNaoHandler(dispatcher)

# Start bot
updater.start_polling(bootstrap_retries=-1)
