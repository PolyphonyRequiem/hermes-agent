"""Explicit, lazy Home work-topic resolution (not yet a model tool).

This module deliberately does not infer jobs from speech or alter gateway
identity. Callers must bind the returned exact origin BEFORE admitting work.
No destination failure permits a DM fallback. Topic keys are explicit stable
identifiers; only a digest, never their raw text, enters public thread names.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import re
import sqlite3

import discord

HOME_CHANNEL_ID = 1513967738656264412
HOME_GUILD_ID = 1512829526391132180


class FollowupTopics:
    """A profile-local topic index with fresh Discord scope/access checks.

    Use one manager per adapter. The database survives hangup/restart; it is
    not a conversation store or a queue. Registry keys include the operator.
    """

    def __init__(self, client, profile_home, operator_id):
        self.client = client
        self.home = Path(profile_home)
        self.operator_id = int(operator_id)
        self._lock = asyncio.Lock()

    async def _parent(self):
        parent = await self.client.fetch_channel(HOME_CHANNEL_ID)
        if not isinstance(parent, discord.TextChannel) or parent.guild.id != HOME_GUILD_ID:
            raise PermissionError('Follow-up Home scope mismatch')
        member = await parent.guild.fetch_member(self.operator_id)
        permissions = parent.permissions_for(member)
        bot = parent.guild.me
        if not permissions.view_channel or not permissions.send_messages_in_threads or bot is None:
            raise PermissionError('Operator or bot cannot access follow-up Home')
        bot_permissions = parent.permissions_for(bot)
        if not bot_permissions.view_channel or not bot_permissions.send_messages_in_threads:
            raise PermissionError('Bot cannot deliver follow-up results')
        return parent, member

    async def validate(self, thread_id):
        parent, member = await self._parent()
        thread = await self.client.fetch_channel(int(thread_id))
        if (not isinstance(thread, discord.Thread)
                or thread.guild.id != HOME_GUILD_ID
                or thread.parent_id != HOME_CHANNEL_ID
                or thread.type != discord.ChannelType.public_thread
                or thread.archived or thread.locked):
            raise PermissionError('Follow-up thread unavailable or outside Home')
        for principal in (member, parent.guild.me):
            permissions = thread.permissions_for(principal)
            if not permissions.view_channel or not permissions.send_messages_in_threads:
                raise PermissionError('Follow-up thread access denied')
        return thread

    async def select(self, topic_key):
        """Resolve an explicitly selected work topic; never call for chit-chat."""
        if not isinstance(topic_key, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', topic_key):
            raise ValueError('Use a stable lower-case work-topic key, not a transcript')
        digest = hashlib.sha256(topic_key.encode()).hexdigest()
        async with self._lock:
            parent, _ = await self._parent()
            self.home.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.home / 'live-followup-topics.sqlite3') as db:
                db.execute('CREATE TABLE IF NOT EXISTS topics (operator TEXT, topic TEXT, thread TEXT, PRIMARY KEY(operator, topic))')
                row = db.execute('SELECT thread FROM topics WHERE operator=? AND topic=?',
                                 (str(self.operator_id), digest)).fetchone()
                if row:
                    thread = await self.validate(row[0])
                else:
                    if not parent.permissions_for(parent.guild.me).create_public_threads:
                        raise PermissionError('Bot cannot create follow-up threads')
                    thread = await parent.create_thread(name='Starvox work ' + digest[:12],
                        type=discord.ChannelType.public_thread, auto_archive_duration=1440,
                        reason='Explicit Starvox follow-up work topic')
                    thread = await self.validate(thread.id)
                    db.execute('INSERT INTO topics VALUES (?, ?, ?)',
                               (str(self.operator_id), digest, str(thread.id)))
            return {'platform': 'discord', 'chat_id': str(HOME_CHANNEL_ID),
                    'thread_id': str(thread.id)}
