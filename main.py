#!/usr/env/python3
#
# This file is part of YeenerBot, licensed under MIT
#
# Copyright (c) 2018 Emily Lau
# Copyright (c) 2018 Sylvia van Os
#
# See LICENSE for more information

import logging
import os
import random

from distutils.util import strtobool

import dataset

from telegram import ParseMode, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Unauthorized
from telegram.ext import CallbackQueryHandler, CommandHandler, Filters, MessageHandler, Updater

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

def ensure_creator(function):
    def wrapper(self, bot, update, **optional_args):
        member = update.message.chat.get_member(update.message.from_user.id)
        if member.status != 'creator':
            bot.send_message(chat_id=update.message.chat_id, text="You do not have the required permission to do this.")
            return

        return function(self=self, bot=bot, update=update, **optional_args)

    return wrapper


def ensure_admin(function):
    def wrapper(bot, update, **optional_args):
        member = update.message.chat.get_member(update.message.from_user.id)
        if member.status not in ['creator', 'administrator']:
            bot.send_message(chat_id=update.message.chat_id, text="You do not have the required permission to do this.")
            return

        return function(bot=bot, update=update, **optional_args)

    return wrapper

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
    def __init__(self, group_id, welcome_enabled=True, welcome_message=None, description=None, rules=None):
        self.group_id = group_id
        self.welcome_enabled = welcome_enabled
        self.welcome_message = welcome_message
        self.description = description
        self.rules = rules

    @staticmethod
    def get_keys():
        return ['group_id', 'welcome_enabled', 'welcome_message', 'description', 'rules']

    def serialize(self):
        return {_key: getattr(self, _key) for _key in Group.get_keys()}

    def save(self):
        DB.update_group(self)


class ErrorHandler():
    def __init__(self, dispatcher):
        dispatcher.add_error_handler(self.handle_error)

    def handle_error(self, bot, update, error):
        from_user = update.callback_query.from_user if update.callback_query else update.message.from_user
        chat = update.callback_query.message.chat if update.callback_query else update.message.chat
        if type(error) == Unauthorized:
            text = "{}, I don't have permission to PM you. Please click here: {}.".format(from_user.name, 'https://telegram.me/{}?start=rules_{}'.format(bot.name[1:], update.message.chat.id))
            bot.send_message(chat_id=chat.id, text=text)
        else:
            text = "Oh no, something went wrong in {}!\n\nError message: {}".format(chat.title, error)
            bot.send_message(chat_id=Helpers.get_creator(chat).id, text=text)


class Helpers():
    @staticmethod
    def get_creator(chat):
        for admin in chat.get_administrators():
            if admin.status == "creator":
                return admin.user

    @staticmethod
    def list_mods(chat):
        mods = []
        for admin in chat.get_administrators():
            if admin.status == "creator":
                creator = admin.user.name
            else:
                mods.append(admin.user.name)

        mods.sort()
        return ["{} (owner)".format(creator)] + mods


    @staticmethod
    def get_description(bot, chat, group):
        return group.description if group.description else bot.get_chat(chat.id).description


class DebugHandler():
    def __init__(self, dispatcher):
        ping_handler = CommandHandler('ping', DebugHandler.ping)
        dispatcher.add_handler(ping_handler)

    @staticmethod
    def ping(bot, update):
        bot.send_message(chat_id=update.message.chat_id, text="Pong")


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

        data = {'usernames': ", ".join(members),
                'title': update.message.chat.title,
                'invite_link': update.message.chat.invite_link,
                'mods': ", ".join(Helpers.list_mods(update.message.chat)),
                'description': Helpers.get_description(bot, update.message.chat, group),
                'rules_with_start': 'https://telegram.me/{}?start=rules_{}'.format(bot.name[1:], update.message.chat.id)}

        text = group.welcome_message if group.welcome_message else "Hello {usernames}, welcome to {title}! Please make sure to read the /rules by pressing the button below."

        bot.send_message(chat_id=update.message.chat_id,
                         text=text.format(**data),
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Click and press START to read the rules', url=data['rules_with_start'])]]))

class RandomHandler():
    def __init__(self, dispatcher):
        roll_handler = CommandHandler('roll', RandomHandler.roll)
        dispatcher.add_handler(roll_handler)

    @staticmethod
    def roll(bot, update):
        try:
            roll = update.message.text.split(' ', 1)[1]
        except IndexError:
            roll = '1d20'

        try:
            dice = [int(n) for n in roll.split('d', 1)]
        except (IndexError, ValueError):
            bot.send_message(chat_id=update.message.chat_id, text="I can't roll a {}, whatever that is. Give me something like 1d20.".format(roll))
            return

        if dice[0] >= 1000 or dice[1] >= 1000:
            bot.send_message(chat_id=update.message.chat_id, text="Sorry, but I'm limited to 999d999.")
            return

        results = []
        for i in range(0, dice[0]):
            results.append(random.randint(1, dice[1]))

        bot.send_message(chat_id=update.message.chat_id, text="{} = {}".format(" ".join([str(result) for result in results]), str(sum(results))))


class RuleHandler():
    def __init__(self, dispatcher):
        rules_handler = CommandHandler('rules', RuleHandler.send_rules)
        callback_rules_handler = CallbackQueryHandler(RuleHandler.send_rules, pattern='/rules')
        setrules_handler = CommandHandler('setrules', RuleHandler.set_rules)
        dispatcher.add_handler(rules_handler)
        dispatcher.add_handler(callback_rules_handler)
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
        from_user = update.callback_query.from_user if update.callback_query else update.message.from_user
        chat = update.callback_query.message.chat if update.callback_query else update.message.chat

        # Notify owner
        try:
            bot.send_message(chat_id=Helpers.get_creator(chat).id, text="{} just requested the rules for {}.".format(from_user.name, chat.title))
        except Unauthorized:
            pass

        group = DB().get_group(chat.id)

        if not group.rules:
            bot.send_message(chat_id=chat.id, text="No rules set for this group yet. Just don't be a meanie, okay?")
            return

        text = "{}\n\n".format(chat.title)
        description = Helpers.get_description(bot, chat, group)
        if description:
            text += "{}\n\n".format(description)

        text += "The group rules are:\n{}\n\n".format(group.rules)
        text += "Your mods are:\n{}".format("\n".join(Helpers.list_mods(update.message.chat)))

        #bot.send_message(chat_id=chat.id, text="{}, I'm PMing you the rules now.".format(from_user.name))
        bot.send_message(chat_id=from_user.id, text=text)


# Setup
token = os.environ['TELEGRAM_BOT_TOKEN']
updater = Updater(token=token)
dispatcher = updater.dispatcher

# Initialize handler
ErrorHandler(dispatcher)
DebugHandler(dispatcher)
GreetingHandler(dispatcher)
RandomHandler(dispatcher)
RuleHandler(dispatcher)

# Start bot
updater.start_polling()
