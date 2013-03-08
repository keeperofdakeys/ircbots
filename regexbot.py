#!/usr/bin/env python
"""
regexbot: IRC-based regular expression evaluation tool.
Copyright 2010 - 2012 Michael Farrell <http://micolous.id.au>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""


import regex, asyncore, threading, inspect, ctypes, time
from datetime import datetime, timedelta
from configparser_plus import ConfigParserPlus
from sys import argv, exit
from ircasync import *
from subprocess import Popen, PIPE
from copy import copy
from string import maketrans, translate
from Queue import PriorityQueue

DEFAULT_CONFIG = {
	'regexbot': {
		'server': 'localhost',
		'port': DEFAULT_PORT,
		'ipv6': 'no',
		'nick': 'regexbot',
		'channels': '#test',
		'channel_flood_cooldown': 5,
		'global_flood_cooldown': 1,
		'max_messages': 25,
		'max_message_size': 200,
		'version': 'regexbot; https://github.com/micolous/ircbots/',
	}
}

config = ConfigParserPlus(DEFAULT_CONFIG)
try:
	config.readfp(open(argv[1]))
except:
	try:
		config.readfp(open('regexbot.ini'))
	except:
		print "Syntax:"
		print "  %s [config]" % argv[0]
		print ""
		print "If no configuration file is specified or there was an error, it will default to `regexbot.ini'."
		print "If there was a failure reading the configuration, it will display this message."
		exit(1)

# read config
SERVER = config.get('regexbot', 'server')
PORT = config.getint('regexbot', 'port')
IPV6 = config.getboolean('regexbot', 'ipv6')
NICK = config.get('regexbot', 'nick')
CHANNELS = config.get('regexbot', 'channels').split()
VERSION = config.get('regexbot', 'version') + '; %s'
try: VERSION = VERSION % Popen(["git","branch","-v","--contains"], stdout=PIPE).communicate()[0].strip()
except: VERSION = VERSION % 'unknown'
del Popen, PIPE

CHANNEL_FLOOD_COOLDOWN = timedelta(seconds=config.getint('regexbot', 'channel_flood_cooldown'))
GLOBAL_FLOOD_COOLDOWN = timedelta(seconds=config.getint('regexbot', 'global_flood_cooldown'))
MAX_MESSAGES = config.getint('regexbot', 'max_messages')
MAX_MESSAGE_SIZE = config.getint('regexbot', 'max_message_size')
try: NICKSERV_PASS = config.get('regexbot', 'nickserv_pass')
except: NICKSERV_PASS = None

message_buffer = {}
last_message = datetime.now()
last_message_times = {}
flooders = {}
ignore_list = []
channel_list = []
user_timeouts = PriorityQueue()
channel_timeouts = PriorityQueue()

if config.has_section('ignore'):
	for k,v in config.items('ignore'):
		try:
			ignore_list.append(regex.compile(v, regex.I))
		except Exception, ex:
			print "Error compiling regular expression in ignore list (%s):" % k
			print "  %s" % v
			print ex
			exit(1)

for channel in CHANNELS:
	c = channel.lower()
	message_buffer[c] = []
	last_message_times[c] = last_message
	channel_list.append(c)

# main code

def flood_control(channel, when):
	"Implements flood controls.  Returns True if the message should be handled, returns False if the floods are in."
	global last_message, last_message_times
	# get delta
	channel_delta = when - last_message_times[channel]
	global_delta = when - last_message
	
	# update times
	last_message = last_message_times[channel] = when
	
	# think global
	if global_delta < GLOBAL_FLOOD_COOLDOWN:
		print "Global flood protection hit, %s of %s seconds were waited" % (global_delta.seconds, GLOBAL_FLOOD_COOLDOWN.seconds)
		return False
		
	# act local
	if channel_delta < CHANNEL_FLOOD_COOLDOWN:
		print "Local %s flood protection hit, %s of %s seconds were waited" % (channel, channel_delta.seconds, CHANNEL_FLOOD_COOLDOWN.seconds)
		return False
		
	# we're cool.
	return True

def channel_timeout(channel, when):
	while not channel_timeouts.empty() and channel_timeouts.queue[0][0] < datetime.now():
		channel_timeouts.get()
	
	timeout_arg = 4
	found_item = False

	for item in channel_timeouts.queue:
		channel = item[1]['channel']

		if channel != item[1]['channel']:
			continue

		found_item = True

		timeout_arg = item[1]['timeout']

		channel_timeouts.queue.remove(item)
		timeout_arg = timeout_arg + 1

		break

	# make the maximum timeout ~30 minutes
	if timeout_arg > 6:
		timeout_arg = 6
	timeout = when + timedelta(seconds=2**timeout_arg)

	new_item = (timeout, {})
	new_item[1]['channel'] = channel
	new_item[1]['timeout'] = timeout_arg
	channel_timeouts.put(new_item)


	if found_item:
		print "Ignoring message on %s because of a timeout, timeout now %d seconds" % (channel, 2**timeout_arg)
		return True
	else:
		return False

def user_timeout(user, when):
	while not user_timeouts.empty() and user_timeouts.queue[0][0] < datetime.now():
		user_timeouts.get()
	
	timeout_arg = 3
	found_item = False

	for item in user_timeouts.queue:
		user = item[1]['user']

		if user != item[1]['user']:
			continue

		found_item = True

		timeout_arg = item[1]['timeout']

		user_timeouts.queue.remove(item)
		timeout_arg = timeout_arg + 1

		break

	# make the maximum timeout ~30 minutes
	if timeout_arg > 12:
		timeout_arg = 12
	timeout = when + timedelta(seconds=2**timeout_arg)

	new_item = (timeout, {})
	new_item[1]['user'] = user
	new_item[1]['timeout'] = timeout_arg
	user_timeouts.put(new_item)

	if found_item:
		print "Ignoring message from %s because of a timeout, timeout now %d seconds" % (user, 2**timeout_arg)
		return True
	else:
		return False


def handle_ctcp(event, match):
	channel = event.channel.lower()
	global message_buffer, MAX_MESSAGES, channel_list
	if channel in channel_list:
		if event.args[0] == "ACTION":
			message_buffer[channel].append([event.nick, event.text[:MAX_MESSAGE_SIZE], True])
			message_buffer[channel] = message_buffer[channel][-MAX_MESSAGES:]
			return

def handle_msg(event, match):
	global message_buffer, MAX_MESSAGES, last_message, last_message_times, flooders, channel_list
	msg = event.text
	channel = event.channel.lower()
	
	if channel not in channel_list:
		# ignore messages not from our channels
		return
	
	if msg.startswith(NICK):
		lmsg = msg.lower()
		
		if 'help' in lmsg or 'info' in lmsg or '?' in lmsg:
			# now flood protect!
			if not flood_control(channel, event.when):
				return
		
			# give information
			event.reply('%s: I am regexbot, the interactive IRC regular expression tool, originally written by micolous.  Source/docs/version: %s' % (event.nick, VERSION))
			return
			
	str_replace = False
	str_translate = False

	if msg.startswith('s'):
		str_replace = True
	if msg.startswith('y'):
		str_translate = True
	
	valid_separators = ['@','#','%',':',';','/','\xe1']
	separator = '/'
	if (str_replace or str_translate) and len(msg) > 1 and msg[1] in valid_separators:
		separator = msg[1]
	else:
		str_replace = False
		str_translate = False

	if (str_replace or str_translate) and msg[1] == separator:
		for item in ignore_list:
			if item.search(event.origin) != None:
				# ignore list item hit
				print "Ignoring message from %s because of: %s" % (event.origin, item.pattern)
				return
		
		# remove all old entries

		test_channel_timeout = channel_timeout(channel, event.when)
		test_user_timeout = user_timeout(event.nick, event.when)

		if not flood_control(channel, event.when):
			return

		if test_channel_timeout or test_user_timeout:
			return

		if len(message_buffer[channel]) == 0:
			event.reply('%s: message buffer is empty' % event.nick)
			return
		
		# parse string to escape separators
		indexes = []
		escaping = False
		for i in xrange(0,len(msg)):
			c = msg[i]
			if c == '\\':
				# toggle between true and false
				escaping = (escaping != True)
			elif c == separator and not escaping:
				# this is a nonescaped separator
				indexes.append(i)
			else:
				# this is an escaped separator
				escaping = False

		# standardise string, so trailing separator doesn't matter
		if len(indexes) == 2:
			indexes.append(len(msg) - 1)

		if len(indexes) != 3:
			event.reply('%s: invalid expression, not the right amount of separators' % event.nick)
			return

		regexp = msg[indexes[0] + 1 : indexes[1]]
		replacement = msg[indexes[1] + 1 : indexes[2]]
		options = msg[indexes[2] + 1 : ]

		# find messages matching the string
		if len(regexp) == 0:
			event.reply('%s: original string is empty' % event.nick)
			return
		if str_replace:
			ignore_case = 'i' in options
			e = None
			try:
				if ignore_case:
					e = regex.compile(regexp, regex.I)
				else:
					e = regex.compile(regexp)
			except Exception, ex:
				event.reply('%s: failure compiling regular expression: %s' % (event.nick, ex))
				return
			
			# now we have a valid regular expression matcher!
			timeout = time.time() + 10
			for x in range(len(message_buffer[channel])-1, -1, -1):
				if time.time() > timeout: break
				result = [None,None]
				thread = RegexThread(e,replacement,message_buffer[channel][x][1],result)
				thread.start()
				try:
					thread.join(0.1)
					while thread.isAlive():
						thread.raiseExc(TimeoutException)
						time.sleep(0.1)

					if result[0] == None or result[1] == None:
						continue

				except Exception, ex:
					event.reply('%s: failure replacing: %s' % (event.nick, ex))
					return

				new_message = []
				# replace the message in the buffer
				new_message = [message_buffer[channel][x][0],result[1].replace('\n','').replace('\r','')[:MAX_MESSAGE_SIZE], message_buffer[channel][x][2]]
				del message_buffer[channel][x]
				message_buffer[channel].append(new_message)
				
				# now print the new text
				print new_message
				if new_message[2]:
					# action
					event.reply((' * %s %s' % (new_message[0], new_message[1]))[:MAX_MESSAGE_SIZE])
				else:
					# normal message
					event.reply(('<%s> %s' % (new_message[0], new_message[1]))[:MAX_MESSAGE_SIZE])
				return
			

		if str_translate:
			if len(regexp) != len(replacement) or len(regexp) < 1:
				event.reply('%s: Translation is different length!'% event.nick)
				return
			# make translation table
			table = maketrans(regexp, replacement)

			for num in xrange(len(message_buffer[channel])-1, -1, -1):
				# make new message, test if changes occur; if not, continue
				result = translate(message_buffer[channel][num][1], table)
				if result == message_buffer[channel][num][1]:
					continue
				
				# build new message, and insert into buffer
				new_message = [message_buffer[channel][num][0],result.replace('\n','').replace('\r','')[:MAX_MESSAGE_SIZE], message_buffer[channel][num][2]]
				del message_buffer[channel][num]
				message_buffer[channel].append(new_message)

				# print new message and send to server
				print new_message
				if new_message[2]:
					# action
					event.reply((' * %s %s' % (new_message[0], new_message[1]))[:MAX_MESSAGE_SIZE])
				else:
					# normal message
					event.reply(('<%s> %s' % (new_message[0], new_message[1]))[:MAX_MESSAGE_SIZE])
				return

		# no match found
		event.reply('%s: no match found' % event.nick)
			

	else:
		# add to buffer
		message_buffer[channel].append([event.nick, msg[:MAX_MESSAGE_SIZE], False])
		
	# trim the buffer
	message_buffer[channel] = message_buffer[channel][-MAX_MESSAGES:]

def handle_welcome(event, match):
	global NICKSERV_PASS
	# Compliance with most network's rules to set this mode on connect.
	event.connection.usermode("+B")
	if NICKSERV_PASS != None:
		event.connection.todo(['NickServ', 'identify', NICKSERV_PASS])

# from http://stackoverflow.com/questions/323972/is-there-any-way-to-kill-a-thread-in-python/325528#325528
def _async_raise(tid, exctype):
	'''Raises an exception in the threads with id tid'''
	if not inspect.isclass(exctype):
		raise TypeError("Only types can be raised (not instances)")
	res = ctypes.pythonapi.PyThreadState_SetAsyncExc(tid,
												  ctypes.py_object(exctype))
	if res == 0:
		raise ValueError("invalid thread id")
	elif res != 1:
		# "if it returns a number greater than one, you're in trouble,
		# and you should call it again with exc=NULL to revert the effect"
		ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, 0)
		raise SystemError("PyThreadState_SetAsyncExc failed")

class RegexThread(threading.Thread):
	def __init__(self,regex,replace,message,result):
		threading.Thread.__init__(self)
		self.regex = regex
		self.replace = replace
		self.message = message
		self.result = result

	def run(self):
		try:
			self.result[0] = self.regex.search(self.message)
		except MemoryError:
			self.result[0] = None
			return
		if self.result[0] != None:
			self.result[1] = self.regex.sub(self.replace,self.message)

	def raiseExc(self, exctype):
		if not self.isAlive():
			raise threading.ThreadError("the thread is not active")
		_async_raise( self.ident, exctype )

class TimeoutException(Exception):
	pass


irc = IRC(nick=NICK, start_channels=CHANNELS, version=VERSION)
irc.bind(handle_msg, PRIVMSG)
irc.bind(handle_welcome, RPL_WELCOME)
irc.bind(handle_ctcp, CTCP_REQUEST)

irc.make_conn(SERVER, PORT, ipv6=IPV6)
asyncore.loop()

