#!/usr/env/python3
# coding=utf-8
#
# This file is part of YeenerBot, licensed under MIT
#
# Copyright (c) 2018 Emily Lau
# Copyright (c) 2018 Sylvia van Os
#
# See LICENSE for more information

import configparser
import datetime
import io
import json
import logging
import os
import time
import random
import re

from collections import OrderedDict
from copy import deepcopy
from distutils.util import strtobool
from math import ceil

import dataset
import requests
import sqlalchemy

from jinja2.sandbox import ImmutableSandboxedEnvironment
from telegram import ChatAction, ParseMode, InlineKeyboardButton, InlineKeyboardMarkup, Update, Message
from telegram.error import Unauthorized, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, Filters, MessageHandler, Updater
from telegram.ext.dispatcher import run_async

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

# Config parsing
config = configparser.ConfigParser()
config.read('config.ini')

try:
    superadmins = [int(superadmin) for superadmin in config.get('GENERAL', 'Superadmins', fallback="").split(" ")]
except Exception:
    print("No superadmins found or failed to parse the list. Continuing as normal.")
    superadmins = []

if not config.has_option('TOKENS', 'Telegram'):
    print("No Telegram token set in config.ini. Cannot continue.")
    exit(1)

token = config['TOKENS']['Telegram']
saucenao_token = config.get('TOKENS', 'SauceNao', fallback=None)

db_type = config['DATABASE']['Type']
db_host = config['DATABASE']['Host']
db_username = config['DATABASE']['Username']
db_password = config['DATABASE']['Password']
db_name = config['DATABASE']['Name']



def ratelimited(function):
    def wrapper(bot, update, **optional_args):
        if update.message.chat.type != 'private':
            group = DB().get_group(update.message.chat.id)
            if group.commandratelimit:
                group_member = DB().get_groupmember(update.message.chat.id, update.message.from_user.id)
                timediff = time.time() - group_member.lastcommandtime
                if timediff < group.commandratelimit:
                    member = update.message.chat.get_member(update.message.from_user.id)
                    if member.status not in ['creator', 'administrator']:
                        bot.send_message(chat_id=update.effective_chat.id, text="You're too spammy. Try again in {} seconds".format(ceil(group.commandratelimit - timediff)), reply_to_message_id=update.message.message_id)
                        return

                group_member.lastcommandtime = time.time()
                group_member.save()

        return function(bot=bot, update=update, **optional_args)
    return wrapper

def busy_indicator(function):
    def wrapper(bot, update, **optional_args):
        try:
            bot.send_chat_action(update.message.chat.id, ChatAction.TYPING)
        except Exception:
            pass
        return function(bot=bot, update=update, **optional_args)
    return wrapper

def ensure_admin(function):
    def wrapper(bot, update, **optional_args):
        member = update.message.chat.get_member(update.message.from_user.id)
        if member.status not in ['creator', 'administrator']:
            if update.message.from_user.id not in superadmins:
                bot.send_message(chat_id=update.effective_chat.id, text="You do not have the required permission to do this.", reply_to_message_id=update.message.message_id)
                return

            user = DB().get_user(update.message.from_user.id)
            if time.time() - user.sudo_time > 30:
                bot.send_message(chat_id=update.effective_chat.id, text="Permission denied. Are you root? (try /sudo).", reply_to_message_id=update.message.message_id)
                return

        command = update.message.text.split(' ', 1)[0]
        if not (command == '/auditlog' or command.startswith('/auditlog@')):
            group = DB().get_group(update.message.chat.id)
            auditlog = json.loads(group.auditlog)
            auditlog.append({'timestamp': time.time(), 'user': update.message.from_user.id, 'command': update.message.text, 'inreplyto': update.message.reply_to_message.from_user.id if update.message.reply_to_message else None})
            group.auditlog = json.dumps(auditlog)
            group.save()
            if group.controlchannel_id:
                audittext = "[{} UTC] {}{}: {}".format(str(datetime.datetime.utcfromtimestamp(auditlog[-1]['timestamp'])).split(".")[0], member.user.name, " (in reply to {})".format(update.message.reply_to_message.from_user.name) if update.message.reply_to_message else "", auditlog[-1]['command'])
                bot.send_message(chat_id=group.controlchannel_id, text="{}\n\n{}".format(update.message.chat.title, audittext))

        return function(bot=bot, update=update, **optional_args)

    return wrapper

def resolve_chat(function):
    def wrapper(bot, update, **optional_args):
        is_control_channel = False
        for group in DB.get_all_groups():
            if group.controlchannel_id == str(update.message.chat.id):
                is_control_channel = True
                break

        if not is_control_channel and update.message.chat.type != 'private':
            return function(bot=bot, update=update, **optional_args)

        user = update.message.from_user
        chats = []

        superadmin = False
        if user.id in superadmins:
            db_user = DB().get_user(update.message.from_user.id)
            if time.time() - db_user.sudo_time <= 30:
                superadmin = True

        for group in DB.get_all_groups():
            try:
                if is_control_channel and group.controlchannel_id != str(update.message.chat.id):
                    continue

                chat = bot.get_chat(group.group_id)

                if chat.type == 'private':
                    DB.delete_group(group)
                    continue

                if chat.id == update.message.chat_id:
                    continue

                if not superadmin and not chat.get_member(user.id).status in ['creator', 'administrator', 'member']:
                    continue

                chats.append(chat)
            except TelegramError as e:
                if (e.message == "Chat not found"):
                    DB.delete_group(group)

                continue

        if len(chats) == 0:
            if is_control_channel:
                message = "You are not in any chats relevant to this control channel."
            else:
                message = "You are not in any chats known to me."
            bot.send_message(chat_id=update.message.chat_id, text=message, reply_to_message_id=update.message.message_id)
            return

        MessageCache.messages[update.message.chat.id] = update.message
        keyboard_buttons = [InlineKeyboardButton("[ALL CHATS]" if not is_control_channel else "[ALL RELATED CHATS]", callback_data=-1)]
        for chat in chats:
            keyboard_buttons.append(InlineKeyboardButton(chat.title, callback_data=chat.id))
        keyboard = InlineKeyboardMarkup([keyboard_button] for keyboard_button in keyboard_buttons)
        bot.send_message(chat_id=update.message.chat_id, text="Execute {} on which chat?".format(update.message.text), reply_markup=keyboard, reply_to_message_id=update.message.message_id)

    return wrapper

def requires_confirmation(function):
    def wrapper(bot, update, **optional_args):
        if update.message.text.split(' ')[-1] != '--yes-i-really-am-sure':
            cloned_message = deepcopy(update.message)
            cloned_message.text += " --yes-i-really-am-sure"
            MessageCache.messages[update.message.chat.id] = cloned_message
            yes_button = InlineKeyboardButton("Yes, I am sure", callback_data=update.message.chat.id)
            keyboard = InlineKeyboardMarkup([[yes_button]])
            bot.send_message(chat_id=update.message.chat_id, text="Are you really sure you want to run '{}'?".format(update.message.text), reply_markup=keyboard, reply_to_message_id=update.message.message_id)
            return

        # Remove really sure parameter
        update.message.text = ' '.join(update.message.text.split(' ')[:-1])

        return function(bot=bot, update=update, **optional_args)

    return wrapper


class dict_no_keyerror(dict):
    def __missing__(self, key):
        return key

class SupportsFilter():
    types = {}

    @staticmethod
    def add_support(command, telegramFilter):
        if not telegramFilter in SupportsFilter.types:
            SupportsFilter.types[telegramFilter] = []

        if not command in SupportsFilter.types[telegramFilter]:
            SupportsFilter.types[telegramFilter].append(command)


class DB():
    __db = dataset.connect('{}://{}:{}@{}/{}'.format(db_type.lower(), db_username, db_password, db_host, db_name))
    __group_table = __db['group']
    __user_table = __db['user']
    __groupmember_table = __db['groupmember']

    @staticmethod
    def get_group(group_id):
        group_data = DB().__group_table.find_one(group_id=group_id)
        if not group_data:
            group = Group(group_id)
            group.save()
            return group

        filtered_group_data = {_key: group_data[_key] for _key in Group.get_keys() if _key in group_data}
        return Group(**filtered_group_data)

    @staticmethod
    def get_all_groups():
        groups = []
        for group_data in DB().__group_table.all():
            filtered_group_data = {_key: group_data[_key] for _key in Group.get_keys() if _key in group_data}
            groups.append(Group(**filtered_group_data))

        return groups

    @staticmethod
    def update_group(group):
        DB().__group_table.upsert(group.serialize(), ['group_id'], types=Group.get_types())

    @staticmethod
    def migrate_group(group, new_id):
        old_id = group.group_id
        group.group_id = new_id
        DB.update_group(group)
        group.group_id = old_id
        DB.delete_group(group)
        for groupmember in DB.get_all_groupmembers(old_id):
            groupmember.group_id = new_id
            DB.update_groupmember(groupmember)
            groupmember.group_id = old_id
            DB.delete_groupmember(groupmember)

    @staticmethod
    def delete_group(group):
        DB().__group_table.delete(group_id=group.group_id)
        for groupmember in DB.get_all_groupmembers(group.group_id):
            DB.delete_groupmember(groupmember)

    @staticmethod
    def get_user(user_id):
        user_data = DB().__user_table.find_one(user_id=user_id)
        if not user_data:
            user = User(user_id)
            user.save()
            return user

        filtered_user_data = {_key: user_data[_key] for _key in User.get_keys() if _key in user_data}
        return User(**filtered_user_data)

    @staticmethod
    def get_all_users():
        users = []
        for user_data in DB().__user_table.all():
            filtered_user_data = {_key: user_data[_key] for _key in User.get_keys() if _key in user_data}
            users.append(User(**filtered_user_data))

        return users

    @staticmethod
    def update_user(user):
        DB().__user_table.upsert(user.serialize(), ['user_id'], types=User.get_types())

    @staticmethod
    def get_groupmember(group_id, user_id):
        groupmember_data = DB().__groupmember_table.find_one(group_id=group_id, user_id=user_id)
        if not groupmember_data:
            groupmember = GroupMember(group_id, user_id)
            groupmember.save()
            return groupmember

        filtered_groupmember_data = {_key: groupmember_data[_key] for _key in GroupMember.get_keys() if _key in groupmember_data}
        return GroupMember(**filtered_groupmember_data)

    @staticmethod
    def get_all_groupmembers(group_id):
        groupmembers = []
        for groupmember_data in DB().__groupmember_table.find(group_id=group_id):
            filtered_groupmember_data = {_key: groupmember_data[_key] for _key in GroupMember.get_keys() if _key in groupmember_data}
            groupmembers.append(GroupMember(**filtered_groupmember_data))

        return groupmembers

    @staticmethod
    def update_groupmember(groupmember):
        DB().__groupmember_table.upsert(groupmember.serialize(), ['group_id', 'user_id'], types=GroupMember.get_types())

    @staticmethod
    def delete_groupmember(groupmember):
        DB().__groupmember_table.delete(groupmember.serialize(), ['group_id', 'user_id'])


class MessageCache():
    messages = {}


class User():
    def __init__(self, user_id, sudo_time=0):
        self.user_id = user_id
        self.sudo_time = sudo_time

    @staticmethod
    def get_keys():
        return ['user_id', 'sudo_time']

    @staticmethod
    def get_types():
        return {'user_id': sqlalchemy.types.BigInteger,
                'sudo_time': sqlalchemy.types.Float}

    def serialize(self):
        return {_key: getattr(self, _key) for _key in User.get_keys()}

    def save(self):
        DB.update_user(self)


class Group():
    def __init__(self, group_id, welcome_enabled=True, welcome_message=None, forceruleread_enabled=False, description=None, rules=None, relatedchat_ids=None, bullet=None, chamber=None, auditlog=None, controlchannel_id=None, roulettekicks_enabled=False, commandratelimit=0):
        self.group_id = group_id
        self.welcome_enabled = welcome_enabled
        self.welcome_message = welcome_message
        self.forceruleread_enabled = forceruleread_enabled
        self.description = description
        self.rules = rules
        self.relatedchat_ids = relatedchat_ids if relatedchat_ids is not None else json.dumps([])
        self.bullet = bullet if bullet is not None else random.randint(0,6)
        self.chamber = chamber if chamber is not None else 5
        self.auditlog = auditlog if auditlog is not None else json.dumps([])
        self.controlchannel_id = controlchannel_id
        self.roulettekicks_enabled = roulettekicks_enabled
        self.commandratelimit = commandratelimit

    @staticmethod
    def get_keys():
        return ['group_id', 'welcome_enabled', 'welcome_message', 'forceruleread_enabled', 'description', 'rules', 'relatedchat_ids', 'bullet', 'chamber', 'auditlog', 'controlchannel_id', 'roulettekicks_enabled', 'commandratelimit']

    @staticmethod
    def get_types():
        return {'group_id': sqlalchemy.types.BigInteger,
                'welcome_enabled': sqlalchemy.types.Boolean,
                'welcome_message': sqlalchemy.types.Text,
                'forceruleread_enabled': sqlalchemy.types.Boolean,
                'description': sqlalchemy.types.Text,
                'rules': sqlalchemy.types.Text,
                'relatedchat_ids': sqlalchemy.types.Text,
                'bullet': sqlalchemy.types.Integer,
                'chamber': sqlalchemy.types.Integer,
                'auditlog': sqlalchemy.types.Text,
                'controlchannel_id': sqlalchemy.types.BigInteger,
                'roulettekicks_enabled': sqlalchemy.types.Boolean,
                'commandratelimit': sqlalchemy.types.Integer}

    def serialize(self):
        return {_key: getattr(self, _key) for _key in Group.get_keys()}

    def save(self):
        auditlog = json.loads(self.auditlog)
        while len(auditlog) > 25:
            auditlog.pop(0)
        self.auditlog = json.dumps(auditlog)

        DB.update_group(self)


class GroupMember():
    def __init__(self, group_id, user_id, readrules=False, warnings=None, lastcommandtime=0):
        self.group_id = group_id
        self.user_id = user_id
        self.readrules = readrules
        self.warnings = warnings if warnings is not None else json.dumps([])
        self.lastcommandtime = lastcommandtime

    @staticmethod
    def get_keys():
        return ['group_id', 'user_id', 'readrules', 'warnings', 'lastcommandtime']

    @staticmethod
    def get_types():
        return {'group_id': sqlalchemy.types.BigInteger,
                'user_id': sqlalchemy.types.BigInteger,
                'readrules': sqlalchemy.types.Boolean,
                'warnings': sqlalchemy.types.Text,
                'lastcommandtime': sqlalchemy.types.Float}

    def serialize(self):
        return {_key: getattr(self, _key) for _key in GroupMember.get_keys()}

    def save(self):
        DB.update_groupmember(self)


class ErrorHandler():
    def __init__(self, dispatcher):
        dispatcher.add_error_handler(self.handle_error)

    @staticmethod
    def filter_tokens(message):
        for tokentype in config['TOKENS']:
            regex = re.compile(re.escape(config['TOKENS'][tokentype]), re.IGNORECASE)
            message = re.sub(regex, "[censored]", message)
        return message

    def handle_error(self, bot, update, error):
        if not update:
            return

        if type(error) == Unauthorized:
            text = "{}, I don't have permission to PM you. Please click the following link and then press START: {}.".format(update.message.from_user.name, 'https://telegram.me/{}?start=rules_{}'.format(bot.name[1:], update.message.chat.id))
            bot.send_message(chat_id=update.effective_chat.id, text=text, reply_to_message_id=update.message.message_id)
        else:
            text = "An error occured: {}".format(ErrorHandler.filter_tokens(str(error)))
            bot.send_message(chat_id=update.effective_chat.id, text=text, reply_to_message_id=update.message.message_id)


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
        if not chat.invite_link:
            chat.invite_link = bot.export_chat_invite_link(chat.id)

        return chat.invite_link

    @staticmethod
    def get_related_chats(bot, group):
        chats = []
        relatedchat_ids = json.loads(group.relatedchat_ids)
        for relatedchat_id in relatedchat_ids[:]:
            try:
                chats.append(bot.get_chat(relatedchat_id))
            except TelegramError:
                # Bugged chat? Remove right here
                relatedchat_ids.remove(relatedchat_id)
                group.relatedchat_ids = json.dumps(relatedchat_ids)
                group.save()
                continue

        return chats


class CallbackHandler():
    def __init__(self, dispatcher):
        callback_handler = CallbackQueryHandler(CallbackHandler.handle_callback, pass_update_queue=True)
        message_handler = MessageHandler(Filters.private, CallbackHandler.handle_message)
        dispatcher.add_handler(callback_handler)
        dispatcher.add_handler(message_handler, 99999) # Lowest possible priority

    @staticmethod
    @busy_indicator
    def handle_callback(bot, update, update_queue):
        reply_to_message = update.callback_query.message.reply_to_message

        if '_' in update.callback_query.data:
            chat_id, command = update.callback_query.data.split('_', 1)
            try:
                reply_to_message = MessageCache.messages.pop(update.callback_query.message.chat.id)
            except KeyError:
                pass
        else:
            chat_id = update.callback_query.data
            try:
                command = MessageCache.messages.pop(update.callback_query.message.chat.id).text
            except KeyError:
                update.callback_query.answer(text="I'm sorry, but I lost your message. Please retry. Most likely I restarted between sending the command and choosing the chat to send it to.")
                update.callback_query.message.delete()
                return

        update.callback_query.message.delete()
        # We use -1 for "all chats", except in control channels, then it's only "all related control channels"
        control_channels = []
        is_control_channel = False
        for group in DB.get_all_groups():
            if group.controlchannel_id:
                control_channels.append(group.controlchannel_id)
                if group.controlchannel_id == str(update.callback_query.message.chat.id):
                    is_control_channel = True

        chats = []
        if chat_id == str(-1):
            for group in DB.get_all_groups():
                try:
                    if is_control_channel and group.controlchannel_id != str(update.callback_query.message.chat.id):
                        continue

                    chat = bot.get_chat(group.group_id)

                    if chat.type == 'private':
                        DB.delete_group(group)
                        continue

                    if str(chat.id) in control_channels:
                        continue

                    if chat.id == update.callback_query.message.chat_id:
                        continue

                    if not chat.get_member(update.callback_query.from_user.id).status in ['creator', 'administrator', 'member']:
                        continue

                    chats.append(chat)
                except TelegramError as e:
                    if (e.message == "Chat not found"):
                        DB.delete_group(group)

                    continue
        else:
            chats = [bot.get_chat(chat_id)]

        for chat in chats:
            message = Message(message_id=-1, date=datetime.datetime.utcnow(), from_user=update.callback_query.from_user, chat=chat, text=command, bot=bot, reply_to_message=reply_to_message)
            new_update = Update(update_id=-1, message=message)
            new_update._effective_chat = update.callback_query.message.chat  # I sure hope this won't break in a future version: https://github.com/python-telegram-bot/python-telegram-bot/blob/d4b5bd40a5545a238ebd63f7ffcc1811691526b0/telegram/update.py#L96<Paste>
            update_queue.put(new_update)

        if reply_to_message:
            update.callback_query.answer(text='Executing {} on message'.format(command))
        else:
            update.callback_query.answer(text='Sent {} to {}'.format(command, chats[0].title if chat_id != str(-1) else "all chats"))

    @staticmethod
    def handle_message(bot, update):
        if update.update_id == -1:
            return

        supported_commands = []

        if update.message.forward_from and Filters.forwarded in SupportsFilter.types:
            for supported_command in SupportsFilter.types[Filters.forwarded]:
                if supported_command not in supported_commands:
                    supported_commands.append(supported_command)

        if update.message.photo and Filters.photo in SupportsFilter.types:
            for supported_command in SupportsFilter.types[Filters.photo]:
                if supported_command not in supported_commands:
                    supported_commands.append(supported_command)

        if len(supported_commands) == 0:
            return

        MessageCache.messages[update.message.chat_id] = update.message
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('/{}'.format(command), callback_data='{}_/{}'.format(update.message.chat.id, command))] for command in supported_commands])
        bot.send_message(chat_id=update.message.chat_id, text="Execute which command on this message?", reply_markup=keyboard, reply_to_message_id=update.message.message_id)


class DebugHandler():
    def __init__(self, dispatcher):
        ping_handler = CommandHandler('ping', DebugHandler.ping)
        dispatcher.add_handler(ping_handler)

    @staticmethod
    @busy_indicator
    @ratelimited
    @run_async
    def ping(bot, update):
        bot.send_message(chat_id=update.message.chat_id, parse_mode="html", text="<code>• {}</code>".format(random.choices([
            "Pong.",
            "Ha! I win.",
            "Damn, I missed!"
        ], weights=[90,5,5])[0]), reply_to_message_id=update.message.message_id)


class SudoHandler():
    def __init__(self, dispatcher):
        sudo_handler = CommandHandler('sudo', SudoHandler.sudo)
        dispatcher.add_handler(sudo_handler)

    @staticmethod
    @busy_indicator
    def sudo(bot, update):
        if update.message.from_user.id not in superadmins:
            bot.send_message(chat_id=update.message.chat_id, text="{} is not a superadmin. This incident will be reported.".format(update.message.from_user.name), reply_to_message_id=update.message.message_id)
            print("{} ({}) tried to use sudo but was denied".format(update.message.from_user.name, update.message.from_user.id))
            return

        user = DB().get_user(update.message.from_user.id)
        user.sudo_time = time.time()
        user.save()

        bot.send_message(chat_id=update.message.chat_id, text="We trust you have received the usual lecture from the local System Administrator. It usually boils down to these three things:\n\n#1) Respect the privacy of others.\n#2) Think before you type.\n#3) With great power comes great responsibility.\n\n(Superadmin activated for 30 seconds).", reply_to_message_id=update.message.message_id)


class GreetingHandler():
    def __init__(self, dispatcher):
        start_handler = CommandHandler('start', GreetingHandler.start)
        created_handler = MessageHandler(Filters.status_update.chat_created, GreetingHandler.created)
        migrated_handler = MessageHandler(Filters.status_update.migrate, GreetingHandler.migrated)
        welcome_handler = MessageHandler(Filters.status_update.new_chat_members, GreetingHandler.welcome)
        clearwelcome_handler = CommandHandler('clearwelcome', GreetingHandler.clear_welcome)
        setwelcome_handler = CommandHandler('setwelcome', GreetingHandler.set_welcome)
        togglewelcome_handler = CommandHandler('togglewelcome', GreetingHandler.toggle_welcome)
        toggleforceruleread_handler = CommandHandler('toggleforceruleread', GreetingHandler.toggle_forceruleread)
        dispatcher.add_handler(start_handler)
        dispatcher.add_handler(created_handler)
        dispatcher.add_handler(migrated_handler)
        dispatcher.add_handler(welcome_handler)
        dispatcher.add_handler(clearwelcome_handler)
        dispatcher.add_handler(setwelcome_handler)
        dispatcher.add_handler(togglewelcome_handler)
        dispatcher.add_handler(toggleforceruleread_handler)

    @staticmethod
    @busy_indicator
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
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def clear_welcome(bot, update):
        group = DB().get_group(update.message.chat.id)
        group.welcome_message = None
        group.save()
        bot.send_message(chat_id=update.effective_chat.id, text="Welcome message cleared.", reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def set_welcome(bot, update):
        group = DB().get_group(update.message.chat.id)
        text = "Welcome message set."
        try:
            group.welcome_message = update.message.text.split(' ', 1)[1]
            group.save()
        except IndexError:
            text = "You need to give the welcome message in the same message.\n\nExample:\n/setwelcome Hello {{ user.name }}! Welcome to {{ chat.title }}! {% if group.rules and group.forceruleread_enabled and not memberinfo.readrules %}This group requires new members to read the rules before they can send messages. {% endif %}{% if group.rules %}Please make sure to read the /rules by pressing the button below.{% endif %}"

        bot.send_message(chat_id=update.effective_chat.id, text=text, reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def toggle_welcome(bot, update):
        group = DB().get_group(update.message.chat.id)

        try:
            enabled = bool(strtobool(update.message.text.split(' ', 1)[1]))
        except (IndexError, ValueError):
            bot.send_message(chat_id=update.effective_chat.id, text="Current status: {}. Please specify true or false to change.".format(group.welcome_enabled), reply_to_message_id=update.message.message_id)
            return

        group.welcome_enabled = enabled
        group.save()

        bot.send_message(chat_id=update.effective_chat.id, text="Welcome: {}".format(str(enabled)), reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def toggle_forceruleread(bot, update):
        group = DB().get_group(update.message.chat.id)

        try:
            enabled = bool(strtobool(update.message.text.split(' ', 1)[1]))
        except (IndexError, ValueError):
            bot.send_message(chat_id=update.effective_chat.id, text="Current status: {}. Please specify true or false to change.".format(group.forceruleread_enabled), reply_to_message_id=update.message.message_id)
            return

        group.forceruleread_enabled = enabled
        group.save()

        bot.send_message(chat_id=update.effective_chat.id, text="Force rule read: {} (dependency welcome: {}, dependency rules set: {})".format(str(enabled), group.welcome_enabled, group.rules is not None), reply_to_message_id=update.message.message_id)

    @staticmethod
    def created(bot, update):
        DB().get_group(update.message.chat.id)  # ensure creation

    @staticmethod
    def migrated(bot, update):
        group = DB().get_group(update.message.migrate_from_chat_id)
        DB.migrate_group(group, update.message.migrate_to_chat_id)

    @staticmethod
    @busy_indicator
    def welcome(bot, update):
        group = DB().get_group(update.message.chat.id)
        if not group.welcome_enabled:
            return

        # Don't welcome bots (or ourselves)
        members = [member for member in update.message.new_chat_members if not member.is_bot]
        if len(members) == 0:
            return

        if group.welcome_message:
            text = group.welcome_message
        else:
            text = "{% if not memberinfo.readrules %}Hello {{ user.name }} and welcome to {{ chat.title }}.{% if group.rules %}{% if group.forceruleread_enabled %} This group requires new members to read the rules before they can send messages.{% endif %} Please make sure to read the /rules by clicking the button below and pressing start.{% endif %}{% else %}Welcome back to {{ chat.title }}, {{ user.name }}!{% endif %}"

        keyboard = None
        if group.rules:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Click and press START to read the rules', url='https://telegram.me/{}?start=rules_{}'.format(bot.name[1:], update.message.chat.id))]])

        env = ImmutableSandboxedEnvironment()
        for member in members:
            member = update.message.chat.get_member(member.id)
            memberinfo = DB().get_groupmember(update.message.chat_id, member.user.id)
            try:
                formatted_string = env.from_string(text).render({'member': member, 'user': member.user, 'group': group, 'memberinfo': memberinfo, 'chat': update.message.chat})
            except Exception as e:
                formatted_string = e
            bot.send_message(chat_id=update.message.chat_id,
                             text=formatted_string,
                             reply_markup=keyboard)

            if group.rules and group.forceruleread_enabled:
                if not memberinfo.readrules and member.status == 'member':
                    bot.restrict_chat_member(chat_id=update.message.chat_id, user_id=member.user.id, can_send_messages=False)


class GroupStateHandler():
    def __init__(self, dispatcher):
        description_handler = CommandHandler('description', GroupStateHandler.description)
        setdescription_handler = CommandHandler('setdescription', GroupStateHandler.set_description)
        relatedchats_handler = CommandHandler('relatedchats', GroupStateHandler.relatedchats)
        addrelatedchat_handler = CommandHandler('addrelatedchat', GroupStateHandler.add_relatedchat)
        removerelatedchat_handler = CommandHandler('removerelatedchat', GroupStateHandler.remove_relatedchat)
        invitelink_handler = CommandHandler('invitelink', GroupStateHandler.invitelink)
        revokeinvitelink_handler = CommandHandler('revokeinvitelink', GroupStateHandler.revokeinvitelink)
        controlchat_handler = CommandHandler('controlchat', GroupStateHandler.controlchat)
        setcontrolchat_handler = CommandHandler('setcontrolchat', GroupStateHandler.set_controlchat)
        setcommandratelimit_handler = CommandHandler('setcommandratelimit', GroupStateHandler.set_commandratelimit)
        dispatcher.add_handler(description_handler)
        dispatcher.add_handler(setdescription_handler)
        dispatcher.add_handler(relatedchats_handler)
        dispatcher.add_handler(addrelatedchat_handler)
        dispatcher.add_handler(removerelatedchat_handler)
        dispatcher.add_handler(invitelink_handler)
        dispatcher.add_handler(revokeinvitelink_handler)
        dispatcher.add_handler(controlchat_handler)
        dispatcher.add_handler(setcontrolchat_handler)
        dispatcher.add_handler(setcommandratelimit_handler)

    @staticmethod
    @busy_indicator
    @resolve_chat
    def relatedchats(bot, update):
        group = DB().get_group(update.message.chat.id)
        relatedchats = Helpers.get_related_chats(bot, group)
        if relatedchats:
            message = "{}\n\nRelated chats:\n".format(update.message.chat.title)
            related_chats_text = []
            for relatedchat in relatedchats:
                try:
                    group = DB().get_group(relatedchat.id)
                    try:
                        description = Helpers.get_description(bot, relatedchat, group)
                    except TelegramError:
                        pass

                    if not description:
                        description = "No description"

                    try:
                        invitelink = Helpers.get_invite_link(bot, relatedchat)
                    except TelegramError:
                        invitelink = "No invite link available"

                    related_chats_text.append("{}\n\n{}\n\n{}".format(relatedchat.title, description, invitelink))
                except TelegramError:
                    continue

            message += "\n----\n".join(related_chats_text)
            bot.send_message(chat_id=update.message.from_user.id, text=message)
        else:
            bot.send_message(chat_id=update.effective_chat.id, text="There are no known related chats for {}".format(update.message.chat.title), reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def add_relatedchat(bot, update):
        chat_ids = update.message.text.split(' ')[1:]
        if len(chat_ids) == 0:
            chats = []
            for group in DB.get_all_groups():
                try:
                    chat = bot.get_chat(group.group_id)
                    if chat.type == 'private':
                        DB.delete_group(group)
                        continue

                    if chat.id == update.message.chat_id:
                        continue

                    if not chat.get_member(update.message.from_user.id).status in ['creator', 'administrator', 'member']:
                        continue

                    chats.append(chat)
                except TelegramError as e:
                    if (e.message == "Chat not found"):
                        DB.delete_group(group)

                    continue

            if len(chats) == 0:
                bot.send_message(chat_id=update.effective_chat.id, text="Can't find any shared chats. Make sure I'm in the chat you want to link.", reply_to_message_id=update.message.message_id)
                return

            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(chat.title, callback_data='{}_/addrelatedchat {}'.format(update.message.chat.id, chat.id))] for chat in chats])
            bot.send_message(chat_id=update.effective_chat.id, text="Add which chat as a related chat?", reply_markup=keyboard, reply_to_message_id=update.message.message_id)
            return

        group = DB().get_group(update.message.chat.id)
        relatedchat_ids = json.loads(group.relatedchat_ids)
        for chat_id in chat_ids:
            if chat_id not in relatedchat_ids:
                relatedchat_ids.append(chat_id)

        group.relatedchat_ids = json.dumps(relatedchat_ids)
        group.save()

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def remove_relatedchat(bot, update):
        group = DB().get_group(update.message.chat.id)
        relatedchat_ids = json.loads(group.relatedchat_ids)
        chat_ids = update.message.text.split(' ', 1)[1:]
        if len(chat_ids) == 0:
            chats = []
            for chat_id in relatedchat_ids:
                try:
                    chat = bot.get_chat(chat_id)
                    chats.append(chat)
                except TelegramError:
                    continue

            if len(chats) > 0:
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(chat.title, callback_data='{}_/removerelatedchat {}'.format(update.message.chat.id, chat.id))] for chat in chats])
                bot.send_message(chat_id=update.effective_chat.id, text="Remove which chat from related chats?", reply_markup=keyboard, reply_to_message_id=update.message.message_id)
            else:
                bot.send_message(chat_id=update.effective_chat.id, text="There are no known related chats for {}".format(update.message.chat.title), reply_to_message_id=update.message.message_id)
            return

        for chat_id in chat_ids:
            try:
                relatedchat_ids.remove(chat_id)
            except ValueError:
                pass

        group.relatedchat_ids = json.dumps(relatedchat_ids)
        group.save()

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def controlchat(bot, update):
        group = DB().get_group(update.message.chat.id)
        if group.controlchannel_id:
            message = "{}\n\nControl chat:\n{}".format(update.message.chat.title, bot.get_chat(group.controlchannel_id).title)
        else:
            message = "{}\n\nNo known control chat".format(update.message.chat.title)

        bot.send_message(chat_id=update.effective_chat.id, text=message, reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def set_controlchat(bot, update):
        chat_id = update.message.text.split(' ')[1:]
        if len(chat_id) == 0:
            chats = []
            for group in DB.get_all_groups():
                try:
                    chat = bot.get_chat(group.group_id)
                    if chat.type == 'private':
                        DB.delete_group(group)
                        continue

                    if chat.id == update.message.chat_id:
                        continue

                    if not chat.get_member(update.message.from_user.id).status in ['creator', 'administrator']:
                        continue

                    chats.append(chat)

                except TelegramError as e:
                    if (e.message == "Chat not found"):
                        DB.delete_group(group)

                    continue

            if len(chats) == 0:
                bot.send_message(chat_id=update.effective_chat.id, text="Can't find any shared chats. Make sure I'm in the chat you want to link.", reply_to_message_id=update.message.message_id)
                return

            keyboard_buttons = [InlineKeyboardButton("[REMOVE CONTROL CHAT]", callback_data='{}_/setcontrolchat -1'.format(update.message.chat.id))]
            for chat in chats:
                keyboard_buttons.append(InlineKeyboardButton(chat.title, callback_data='{}_/setcontrolchat {}'.format(update.message.chat.id, chat.id)))
            keyboard = InlineKeyboardMarkup([keyboard_button] for keyboard_button in keyboard_buttons)
            bot.send_message(chat_id=update.effective_chat.id, text="Set which chat as a control chat?", reply_markup=keyboard, reply_to_message_id=update.message.message_id)
            return

        group = DB().get_group(update.message.chat.id)
        if chat_id[0] == str(-1):
            group.controlchannel_id = None
        else:
            if not bot.get_chat(chat_id[0]).get_member(update.message.from_user.id).status in ['creator', 'administrator']:
                bot.send_message(chat_id=update.effective_chat.id, text="You need to be an admin in the chat you want to set as control chat.", reply_to_message_id=update.message.message_id)
                return

            group.controlchannel_id = chat_id[0]
        group.save()

    @staticmethod
    @busy_indicator
    @resolve_chat
    def description(bot, update):
        group = DB().get_group(update.message.chat.id)
        description = Helpers.get_description(bot, update.message.chat, group)
        if not description:
            description = "No description"

        bot.send_message(chat_id=update.message.from_user.id, text = "{}\n\n{}".format(update.message.chat.title, description))

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def set_description(bot, update):
        group = DB().get_group(update.message.chat.id)
        text = "Description set."
        try:
            group.description = update.message.text.split(' ', 1)[1]
        except IndexError:
            group.description = None
            text = "Description reset to default (fallback to Telegram description)."

        group.save()

        bot.send_message(chat_id=update.effective_chat.id, text=text, reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @run_async
    def invitelink(bot, update):
        invite_link = Helpers.get_invite_link(bot, update.message.chat)
        if not invite_link:
            bot.send_message(chat_id=update.effective_chat.id, text="{} does not have an invite link".format(update.message.chat.title), reply_to_message_id=update.message.message_id)
            return

        bot.send_message(chat_id=update.effective_chat.id, text="Invite link for {} is {}".format(update.message.chat.title, invite_link), reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    @run_async
    def revokeinvitelink(bot, update):
        bot.export_chat_invite_link(update.message.chat.id)
        bot.send_message(chat_id=update.effective_chat.id, text="Invite link for {} revoked".format(update.message.chat.title), reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def set_commandratelimit(bot, update):
        group = DB().get_group(update.message.chat.id)
        text = "Member can now only execute one fun command per {} seconds."
        try:
            group.commandratelimit = int(update.message.text.split(' ', 1)[1])
        except (IndexError, TypeError):
            group.commandratelimit = 0
            text = "Command rate limit reset to default ({} seconds)."

        group.save()

        bot.send_message(chat_id=update.effective_chat.id, text=text.format(group.commandratelimit), reply_to_message_id=update.message.message_id)


class RandomHandler():
    def __init__(self, dispatcher):
        roll_handler = CommandHandler('roll', RandomHandler.roll)
        flip_handler = CommandHandler('flip', RandomHandler.flip)
        shake_handler = CommandHandler('shake', RandomHandler.shake)
        roulette_handler = CommandHandler('roulette', RandomHandler.roulette)
        toggleroulettekicks_handler = CommandHandler('toggleroulettekicks', RandomHandler.toggle_roulettekicks)
        dispatcher.add_handler(roll_handler)
        dispatcher.add_handler(flip_handler)
        dispatcher.add_handler(shake_handler)
        dispatcher.add_handler(roulette_handler)
        dispatcher.add_handler(toggleroulettekicks_handler)

    @staticmethod
    @busy_indicator
    @ratelimited
    @run_async
    def roll(bot, update):
        try:
            roll = update.message.text.split(' ', 2)[1]
            dice = [int(n) for n in roll.split('d', 1)]
        except (IndexError, ValueError):
            dice = [1, 20]

        if dice[0] < 1 or dice[1] < 1:
            bot.send_message(chat_id=update.message.chat_id, text="Very funny.", reply_to_message_id=update.message.message_id)
            return

        if dice[1] == 1:
            bot.send_message(chat_id=update.message.chat_id, text="I'm here to roll dice, not calculate 1+1...", reply_to_message_id=update.message.message_id)
            return

        if dice[0] > 100 or dice[1] > 100:
            bot.send_message(chat_id=update.message.chat_id, text="Sorry, but I'm limited to 100d100.", reply_to_message_id=update.message.message_id)
            return

        if dice[0] == 1:
            bot.send_message(chat_id=update.message.chat_id, text=str(random.randint(1, dice[1])), reply_to_message_id=update.message.message_id)
            return

        results = []
        for i in range(0, dice[0]):
            results.append(random.randint(1, dice[1]))

        bot.send_message(chat_id=update.message.chat_id, text="{} = {}".format(" + ".join([str(result) for result in results]), str(sum(results))), reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @ratelimited
    @run_async
    def flip(bot, update):
        bot.send_message(chat_id=update.message.chat_id, parse_mode="html", text="<code>• {}</code>".format(random.choices([
            "Heads.",
            "Tails.",
            "The coin has landed sideways."
        ], weights=[45,45,10])[0]), reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @ratelimited
    @run_async
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
        ])), reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @ratelimited
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
            bot.send_message(chat_id=update.message.chat_id, parse_mode="html", text="<code>• *BOOM!* Your brain is now all over the wall behind you.</code>", reply_to_message_id=update.message.message_id)
            group.bullet = random.randint(0,6)
            group.chamber = 5
            group.save()
            if not group.roulettekicks_enabled:
                return

            if update.message.chat.type == 'private':
                return

            for admin in update.message.chat.get_administrators():
                if admin.user.id == update.message.from_user.id:
                    return
            
            try:
                bot.send_message(chat_id=update.message.from_user.id, text=Helpers.get_invite_link(bot, update.message.chat))
                bot.kick_chat_member(chat_id=update.message.chat_id, user_id=update.message.from_user.id)
                bot.send_message(chat_id=update.message.chat_id, text="{} is no longer among us.".format(update.message.from_user.name))
            except TelegramError:
                return

            bot.unban_chat_member(chat_id=update.message.chat_id, user_id=update.message.from_user.id)
        elif group.chamber == 5:
            bot.send_message(chat_id=update.message.chat_id, parse_mode="html", text="<code>• *Click!* Oh, I forgot to load the gun...</code>", reply_to_message_id=update.message.message_id)
            group.bullet = random.randint(0,5)
            group.chamber = 5
            group.save()
        else:
            chambersremaining = 5 - group.chamber
            bot.send_message(chat_id=update.message.chat_id, parse_mode="html", text="<code>• *Click* You're safe. For now.\n{} chamber{} remaining.</code>".format(chambersremaining,"s" if chambersremaining != 1 else ""), reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def toggle_roulettekicks(bot, update):
        group = DB().get_group(update.message.chat.id)

        try:
            enabled = bool(strtobool(update.message.text.split(' ', 1)[1]))
        except (IndexError, ValueError):
            bot.send_message(chat_id=update.effective_chat.id, text="Current status: {}. Please specify true or false to change.".format(group.roulettekicks_enabled), reply_to_message_id=update.message.message_id)
            return

        group.roulettekicks_enabled = enabled
        group.save()

        bot.send_message(chat_id=update.effective_chat.id, text="Roulette kicks: {}".format(str(enabled)), reply_to_message_id=update.message.message_id)


class RuleHandler():
    def __init__(self, dispatcher):
        rules_handler = CommandHandler('rules', RuleHandler.send_rules)
        clearrules_handler = CommandHandler('clearrules', RuleHandler.clear_rules)
        setrules_handler = CommandHandler('setrules', RuleHandler.set_rules)
        dispatcher.add_handler(rules_handler)
        dispatcher.add_handler(clearrules_handler)
        dispatcher.add_handler(setrules_handler)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def clear_rules(bot, update):
        group = DB().get_group(update.message.chat.id)
        group.rules = None
        group.save()
        bot.send_message(chat_id=update.effective_chat.id, text="Rules cleared.", reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def set_rules(bot, update):
        group = DB().get_group(update.message.chat.id)
        text = "Rules set."
        try:
            group.rules = update.message.text.split(' ', 1)[1]
            group.save()
        except IndexError:
            text = "You need to give the rules in the same message.\n\nExample:\n/setrules The only rule is that there are no rules. Except this one."

        bot.send_message(chat_id=update.effective_chat.id, text=text, reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @resolve_chat
    def send_rules(bot, update):
        group = DB().get_group(update.message.chat.id)
        groupmember = DB().get_groupmember(update.message.chat_id, update.message.from_user.id)

        if not groupmember.readrules:
            member = update.message.chat.get_member(update.message.from_user.id)
            if member.status == 'restricted':
                bot.restrict_chat_member(chat_id=update.message.chat_id, user_id=update.message.from_user.id, can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True)

            groupmember.readrules = True
            groupmember.save()

        if not group.rules:
            bot.send_message(chat_id=update.effective_chat.id, text="No rules set for this group yet. Just don't be a meanie, okay?", reply_to_message_id=update.message.message_id)
            return

        text = "{}\n\n".format(update.message.chat.title)
        description = Helpers.get_description(bot, update.message.chat, group)
        if description:
            text += "{}\n\n".format(description)

        text += "The group rules are:\n{}\n\n".format(group.rules)
        text += "Your mods are:\n{}".format("\n".join(Helpers.list_mods(update.message.chat)))

        relatedchats = Helpers.get_related_chats(bot, group)
        if relatedchats:
            text += "\n\nRelated chats:\n"
            related_chats_text = []
            for relatedchat in relatedchats:
                try:
                    group = DB().get_group(relatedchat.id)

                    try:
                        description = Helpers.get_description(bot, relatedchat, group)
                    except TelegramError:
                        pass

                    if not description:
                        description = "No description"

                    try:
                        invitelink = Helpers.get_invite_link(bot, relatedchat)
                    except TelegramError:
                        invitelink = "No invite link available"

                    related_chats_text.append("{}\n\n{}\n\n{}".format(relatedchat.title, description, invitelink))
                except TelegramError:
                    continue

            text += "\n----\n".join(related_chats_text)

        bot.send_message(chat_id=update.message.from_user.id, text=text)

class ModerationHandler():
    def __init__(self, dispatcher):
        auditlog_handler = CommandHandler('auditlog', ModerationHandler.auditlog)
        warnings_handler = CommandHandler('warnings', ModerationHandler.warnings)
        SupportsFilter.add_support('warnings', Filters.forwarded)
        warn_handler = CommandHandler('warn', ModerationHandler.warn)
        SupportsFilter.add_support('warn', Filters.forwarded)
        clearwarnings_handler = CommandHandler('clearwarnings', ModerationHandler.clearwarnings)
        SupportsFilter.add_support('clearwarnings', Filters.forwarded)
        kick_handler = CommandHandler('kick', ModerationHandler.kick)
        SupportsFilter.add_support('kick', Filters.forwarded)
        ban_handler = CommandHandler('ban', ModerationHandler.ban)
        SupportsFilter.add_support('ban', Filters.forwarded)
        say_handler = CommandHandler('say', ModerationHandler.say)
        call_mods_handler = CommandHandler('admins', ModerationHandler.call_mods)
        call_mods_handler2 = CommandHandler('mods', ModerationHandler.call_mods)
        dispatcher.add_handler(auditlog_handler)
        dispatcher.add_handler(warnings_handler)
        dispatcher.add_handler(warn_handler)
        dispatcher.add_handler(clearwarnings_handler)
        dispatcher.add_handler(kick_handler)
        dispatcher.add_handler(ban_handler)
        dispatcher.add_handler(say_handler)
        dispatcher.add_handler(call_mods_handler)
        dispatcher.add_handler(call_mods_handler2)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    def auditlog(bot, update):
        group = DB().get_group(update.message.chat.id)
        auditlog = json.loads(group.auditlog)
        if len(auditlog) == 0:
            bot.send_message(chat_id=update.message.from_user.id, text="No admin actions have been logged in this chat yet.")
            return

        audittext = "{} most recent admin events in {}:".format(len(auditlog), update.message.chat.title)
        for auditentry in reversed(auditlog):
            try:
                member = update.message.chat.get_member(auditentry['user'])
            except TelegramError:
                # If we can't find the user in the chat anymore, assume they're no longer a mod.
                continue

            # Old auditentries lack inreplyto, don't crash
            if 'inreplyto' not in auditentry:
                auditentry['inreplyto'] = None

            if auditentry['inreplyto']:
                try:
                    auditentry['inreplyto'] = update.message.chat.get_member(auditentry['inreplyto']).user.name
                except TelegramError:
                    pass

            audittext += "\n[{} UTC] {}{}: {}".format(str(datetime.datetime.utcfromtimestamp(auditentry['timestamp'])).split(".")[0], member.user.name, " (in reply to {})".format(auditentry['inreplyto']) if auditentry['inreplyto'] else "", auditentry['command'])

        bot.send_message(chat_id=update.message.from_user.id, text=audittext)


    @staticmethod
    @busy_indicator
    @resolve_chat
    def warnings(bot, update):
        if update.message.reply_to_message:
            message = update.message.reply_to_message
        else:
            message = update.message

        groupmember = DB().get_groupmember(update.message.chat.id, message.from_user.id)
        warnings = json.loads(groupmember.warnings)
        if not warnings:
            bot.send_message(chat_id=update.effective_chat.id, text='{} has not received any warnings in this chat.'.format(message.from_user.name), reply_to_message_id=update.message.message_id)
            return

        warningtext = "{} has received the following warnings since they joined:\n".format(message.from_user.name)
        for warning in reversed(warnings):
            try:
                warnedby = update.message.chat.get_member(warning['warnedby'])
            except TelegramError:
                # If we can't find the warner in the chat anymore, assume they're no longer a mod and the warning is invalid.
                continue

            warningtext += "\n[{} UTC] warned by {} (reason: {})".format(str(datetime.datetime.utcfromtimestamp(warning['timestamp'])).split(".")[0], warnedby.user.name, warning['reason'] if warning['reason'] else "none given")

        bot.send_message(chat_id=update.effective_chat.id, text=warningtext, reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @ensure_admin
    def warn(bot, update):
        if not update.message.reply_to_message:
            bot.send_message(chat_id=update.message.chat.id, text="Reply to a message to warn the person who wrote it.", reply_to_message_id=update.message.message_id)
            return

        if update.message.reply_to_message.from_user.id == bot.id:
            bot.send_message(chat_id=update.message.chat.id, text=random.choice(["What did I even do!", "I'm just trying to help!", "Have you checked /auditlog to find the real culprit?", "I-I'm sorry..."]), reply_to_message_id=update.message.message_id)
            return

        message = update.message.reply_to_message
        groupmember = DB().get_groupmember(update.message.chat.id, message.from_user.id)
        warnings = json.loads(groupmember.warnings)

        try:
            reason = update.message.text.split(' ', 1)[1]
        except IndexError:
            reason = None

        warnings.append({'timestamp': time.time(), 'reason': reason, 'warnedby': update.message.from_user.id})
        groupmember.warnings = json.dumps(warnings)
        groupmember.save()

        warningtext = "{}, you just received a warning. Here are all warnings since you joined:\n".format(message.from_user.name)
        for warning in reversed(warnings):
            try:
                warnedby = update.message.chat.get_member(warning['warnedby'])
            except TelegramError:
                # If we can't find the warner in the chat anymore, assume they're no longer a mod and the warning is invalid.
                continue

            warningtext += "\n[{} UTC] warned by {} (reason: {})".format(str(datetime.datetime.utcfromtimestamp(warning['timestamp'])).split(".")[0], warnedby.user.name, warning['reason'] if warning['reason'] else "none given")

        bot.send_message(chat_id=update.message.chat.id, text=warningtext, reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @ensure_admin
    def clearwarnings(bot, update):
        if not update.message.reply_to_message:
            bot.send_message(chat_id=update.message.chat.id, text="Reply to a message to clear the warnings of the person who wrote it.", reply_to_message_id=update.message.message_id)
            return

        message = update.message.reply_to_message
        groupmember = DB().get_groupmember(update.message.chat.id, message.from_user.id)
        warnings = json.loads(groupmember.warnings)
        warnings = []
        groupmember.warnings = json.dumps(warnings)
        groupmember.save()

        bot.send_message(chat_id=update.message.chat.id, text="Warnings of user {} cleared.".format(message.from_user.name), reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @ensure_admin
    def kick(bot, update):
        if not update.message.reply_to_message:
            bot.send_message(chat_id=update.message.chat.id, text="Reply to a message to kick the person who wrote it.", reply_to_message_id=update.message.message_id)
            return

        message = update.message.reply_to_message
        groupmember = DB().get_groupmember(update.message.chat.id, message.from_user.id)
        warnings = json.loads(groupmember.warnings)

        try:
            reason = '[KICK] {}'.format(update.message.text.split(' ', 1)[1])
        except IndexError:
            reason = '[KICK]'

        warnings.append({'timestamp': time.time(), 'reason': reason, 'warnedby': update.message.from_user.id})
        groupmember.warnings = json.dumps(warnings)
        groupmember.save()

        bot.kick_chat_member(chat_id=message.chat_id, user_id=message.from_user.id)
        bot.unban_chat_member(chat_id=message.chat_id, user_id=message.from_user.id)
        bot.send_message(chat_id=update.message.chat.id, text="I've kicked {}.".format(message.from_user.name), reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @ensure_admin
    def ban(bot, update):
        if not update.message.reply_to_message:
            bot.send_message(chat_id=update.message.chat.id, text="Reply to a message to ban the person who wrote it.", reply_to_message_id=update.message.message_id)
            return

        message = update.message.reply_to_message
        groupmember = DB().get_groupmember(update.message.chat.id, message.from_user.id)
        warnings = json.loads(groupmember.warnings)

        try:
            reason = '[BAN] {}'.format(update.message.text.split(' ', 1)[1])
        except IndexError:
            reason = '[BAN]'

        warnings.append({'timestamp': time.time(), 'reason': reason, 'warnedby': update.message.from_user.id})
        groupmember.warnings = json.dumps(warnings)
        groupmember.save()

        bot.kick_chat_member(chat_id=message.chat_id, user_id=message.from_user.id)
        bot.send_message(chat_id=update.message.chat.id, text="I've banned {}.".format(message.from_user.name), reply_to_message_id=update.message.message_id)

    @staticmethod
    @busy_indicator
    @resolve_chat
    @ensure_admin
    @run_async
    def say(bot, update):
        bot.send_message(chat_id=update.message.chat_id, text=" ".join(update.message.text.split(' ')[1:]))

    @staticmethod
    @busy_indicator
    @requires_confirmation
    @run_async
    def call_mods(bot, update):
        bot.send_message(chat_id=update.message.chat_id, text="{}, anyone there? {} believes there's a serious issue going on that needs moderator attention. Please check ASAP!".format(", ".join(admin.user.name for admin in update.message.chat.get_administrators() if not admin.user.is_bot), update.message.from_user.name), reply_to_message_id=update.message.message_id)


class SauceNaoHandler():
    def __init__(self, dispatcher):
        saucenao_handler = CommandHandler('source', SauceNaoHandler.get_source)
        SupportsFilter.add_support('source', Filters.photo)
        dispatcher.add_handler(saucenao_handler)

    @staticmethod
    @busy_indicator
    @run_async
    def get_source(bot, update):
        if not update.message.reply_to_message:
            bot.send_message(chat_id=update.message.chat.id, text="You didn't reply to the message you want the source of.", reply_to_message_id=update.message.message_id)
            return

        message = update.message.reply_to_message
        if len(message.photo) == 0:
            bot.send_message(chat_id=update.message.chat.id, text="I see no picture here.", reply_to_message_id=update.message.message_id)
            return

        picture = bot.get_file(file_id=message.photo[-1].file_id)
        picture_data = io.BytesIO()
        picture.download(out=picture_data)
        request_url = 'https://saucenao.com/search.php?output_type=2&numres=1&api_key={}'.format(saucenao_token)
        r = requests.post(request_url, files={'file': ("image.png", picture_data.getvalue())})
        if r.status_code != 200:
            bot.send_message(chat_id=update.message.chat.id, text="SauceNao failed me :( HTTP {}".format(r.status_code), reply_to_message_id=update.message.message_id)
            return

        result_data = json.JSONDecoder(object_pairs_hook=OrderedDict).decode(r.text)
        if int(result_data['header']['results_returned']) == 0:
            bot.send_message(chat_id=update.message.chat.id, text="Couldn't find a source :(", reply_to_message_id=update.message.message_id)
            return

        results = sorted(result_data['results'], key=lambda result: float(result['header']['similarity']))

        bot.send_message(chat_id=update.message.chat.id, text="I'm {}% sure this is the source: {}".format(results[-1]['header']['similarity'], results[-1]['data']['ext_urls'][0]), reply_to_message_id=update.message.message_id)


# Setup
updater = Updater(token=token)
dispatcher = updater.dispatcher

# Initialize handler
ErrorHandler(dispatcher)
CallbackHandler(dispatcher)
DebugHandler(dispatcher)
SudoHandler(dispatcher)
GreetingHandler(dispatcher)
GroupStateHandler(dispatcher)
RandomHandler(dispatcher)
RuleHandler(dispatcher)
ModerationHandler(dispatcher)
if saucenao_token:
    SauceNaoHandler(dispatcher)
else:
    print("No SauceNao token set in config.ini. SauceNaoHandler will be disabled.")

# Start bot
updater.start_polling(bootstrap_retries=-1)
