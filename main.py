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

from distutils.util import strtobool

import dataset

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
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
    def wrapper(self, bot, update, **optional_args):
        member = update.message.chat.get_member(update.message.from_user.id)
        if member.status not in ['creator', 'administrator']:
            bot.send_message(chat_id=update.message.chat_id, text="You do not have the required permission to do this.")
            return

        return function(self=self, bot=bot, update=update, **optional_args)

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
        if type(error) == Unauthorized:
            text = "I don't have permission to PM you, please click my profile, type /start in a PM and try again."
        else:
            text = "Oh no, something went wrong!"

        text += "\n\nError message: {}".format(error)

        bot.send_message(chat_id=update.message.chat_id, text=text)

class GreetingHandler():
    def __init__(self, dispatcher):
        welcome_handler = MessageHandler(Filters.status_update.new_chat_members, self.welcome)
        setwelcome_handler = CommandHandler('setwelcome', self.set_welcome)
        togglewelcome_handler = CommandHandler('togglewelcome', self.toggle_welcome)
        dispatcher.add_handler(welcome_handler)
        dispatcher.add_handler(setwelcome_handler)
        dispatcher.add_handler(togglewelcome_handler)

    @ensure_admin
    def set_welcome(self, bot, update):
        group = DB().get_group(update.message.chat.id)
        text = "Welcome message set."
        try:
            group.welcome_message = update.message.text.split(' ', 1)[1]
        except IndexError:
            group.welcome_message = None
            text = "Welcome message reset to default."

        group.save()

        bot.send_message(chat_id=update.message.chat_id, text=text)

    @ensure_admin
    def toggle_welcome(self, bot, update):
        try:
            enabled = bool(strtobool(update.message.text.split(' ', 1)[1]))
        except (IndexError, ValueError):
            bot.send_message(chat_id=update.message.chat_id, text="Please specify true or false")
            return

        group = DB().get_group(update.message.chat.id)
        group.welcome_enabled = enabled
        group.save()

        bot.send_message(chat_id=update.message.chat_id, text="Welcome: {}".format(str(enabled)))

    def welcome(self, bot, update):
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
                'mods': ", ".join([admin.user.name for admin in update.message.chat.get_administrators()]),
                'description': group.description if group.description else update.message.chat.description}

        text = group.welcome_message if group.welcome_message else "Hello {usernames}, welcome to {title}! Please make sure to read the rules."

        bot.send_message(chat_id=update.message.chat_id,
                         text=text.format(**data),
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Read the rules', callback_data='/rules')]]))


class RuleHandler():
    def __init__(self, dispatcher):
        rules_handler = CommandHandler('rules', self.send_rules)
        callback_rules_handler = CallbackQueryHandler(self.send_rules, pattern='/rules')
        setrules_handler = CommandHandler('setrules', self.set_rules)
        dispatcher.add_handler(rules_handler)
        dispatcher.add_handler(callback_rules_handler)
        dispatcher.add_handler(setrules_handler)

    @ensure_admin
    def set_rules(self, bot, update):
        group = DB().get_group(update.message.chat.id)
        text = "Rules set."
        try:
            group.rules = update.message.text.split(' ', 1)[1]
        except IndexError:
            group.rules = None
            text = "Rules removed."

        group.save()

        bot.send_message(chat_id=update.message.chat_id, text=text)

    def send_rules(self, bot, update):
        from_user = update.callback_query.from_user if update.callback_query else update.message.from_user

        rules = DB().get_group(update.message.chat.id).rules

        if not rules:
            bot.send_message(chat_id=update.message.chat_id, text="No rules set for this group yet. Just don't be a meanie, okay?")
            return

        rules += "\n\nYour mods are: {}".format("\n".join(admin.user.name for admin in update.message.chat.get_administrators()))
        bot.send_message(chat_id=from_user.id, text=rules)


# Setup
token = os.environ['TELEGRAM_BOT_TOKEN']
updater = Updater(token=token)
dispatcher = updater.dispatcher

# Initialize handler
ErrorHandler(dispatcher)
GreetingHandler(dispatcher)
RuleHandler(dispatcher)

# Start bot
updater.start_polling()
