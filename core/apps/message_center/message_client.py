#!/usr/bin/env python
# -*- coding: UTF-8 -*-
'''=================================================
@Author ：zk.wang
@Date   ：2020/3/16 
=================================================='''
import json
import logging
from django.contrib.auth.models import User
from kubeops_api.models.item import Item
from .models import Message, UserNotificationConfig, UserReceiver, UserMessage
from kubeops_api.models.setting import Setting
from ko_notification_utils.email_smtp import Email
from ko_notification_utils.ding_talk import DingTalk
from .message_thread import MessageThread
from django.template import Template, Context, loader

logger = logging.getLogger('kubeops')


class MessageClient():

    def __init__(self):
        pass

    def get_receivers(self, item_id):
        receivers = []
        admin = User.objects.filter(is_superuser=1)
        receivers.extend(list(admin))

        if item_id is not None:
            item = Item.objects.get(id=item_id)
            profiles = item.profiles.all()
            for profile in profiles:
                receivers.append(profile.user)

        return receivers

    def split_receiver_by_send_type(self, receivers, type):
        messageReceivers = []
        setting_email_enable = False
        email_receivers = ''
        if Setting.objects.get(key='SMTP_STATUS') and Setting.objects.get(key='SMTP_STATUS').value == 'ENABLE':
            setting_email_enable = True
        send_ding_talk_enable = False
        ding_talk_receivers = ''
        if Setting.objects.get(key='DINGTALK_STATUS') and Setting.objects.get(key='DINGTALK_STATUS').value == 'ENABLE':
            send_ding_talk_enable = True

        for receiver in receivers:
            config = UserNotificationConfig.objects.get(type=type, user_id=receiver.id)
            user_receiver = UserReceiver.objects.get(user_id=receiver.id)
            if config.vars['LOCAL'] == 'ENABLE':
                messageReceivers.append(
                    MessageReceiver(user_id=receiver.id, receive=receiver.username, send_type='LOCAL'))

            if config.vars['WORKWEIXIN'] == 'ENABLE' and user_receiver.vars['WORKWEIXIN'] != '':
                messageReceivers.append(
                    MessageReceiver(user_id=receiver.id, receive=user_receiver.vars['WORKWEIXIN'],
                                    send_type='WORKWEIXIN'))
            if setting_email_enable and config.vars['EMAIL'] == 'ENABLE' and user_receiver.vars['EMAIL'] != '':
                if email_receivers != '':
                    email_receivers = email_receivers + ',' + receiver.email
                else:
                    email_receivers = receiver.email

            if send_ding_talk_enable and config.vars['DINGTALK'] == 'ENABLE' and user_receiver.vars['DINGTALK'] != '':
                if ding_talk_receivers != '':
                    ding_talk_receivers = ding_talk_receivers + ',' + user_receiver.vars['DINGTALK']
                else:
                    ding_talk_receivers = user_receiver.vars['DINGTALK']

        if len(email_receivers) > 0:
            messageReceivers.append(
                MessageReceiver(user_id=1, receive=email_receivers, send_type='EMAIL'))

        if len(ding_talk_receivers) > 0:
            messageReceivers.append(
                MessageReceiver(user_id=1, receive=ding_talk_receivers, send_type='DINGTALK')
            )

        return messageReceivers

    def insert_message(self, message):
        title = message.get('title', None)
        item_id = message.get('item_id', None)
        content = message.get('content', None)
        type = message.get('type', None)
        level = message.get('level', None)
        message = Message.objects.create(title=title, content=json.dumps(content), type=type, level=level)
        message_receivers = self.split_receiver_by_send_type(receivers=self.get_receivers(item_id), type=type)
        user_messages = []
        for message_receiver in message_receivers:
            user_message = UserMessage(receive=message_receiver.receive, user_id=message_receiver.user_id,
                                       send_type=message_receiver.send_type,
                                       read_status=UserMessage.MESSAGE_READ_STATUS_UNREAD,
                                       receive_status=UserMessage.MESSAGE_RECEIVE_STATUS_WAITING, message_id=message.id)
            user_messages.append(user_message)
        UserMessage.objects.bulk_create(user_messages)
        thread = MessageThread(func=send_email, message_id=message.id)
        thread.start()
        thread2 = MessageThread(func=send_ding_talk_msg, message_id=message.id)
        thread2.start()


def send_email(message_id):
    user_message = UserMessage.objects.get(message_id=message_id, send_type=UserMessage.MESSAGE_SEND_TYPE_EMAIL)
    setting_email = Setting.get_settings("email")
    email = Email(address=setting_email['SMTP_ADDRESS'], port=setting_email['SMTP_PORT'],
                  username=setting_email['SMTP_USERNAME'], password=setting_email['SMTP_PASSWORD'])
    res = email.send_html_mail(receiver=user_message.receive, title=user_message.message.title,
                               content=get_email_content(user_message))
    if res.success:
        user_message.receive_status = UserMessage.MESSAGE_RECEIVE_STATUS_SUCCESS
        user_message.save()
    else:
        logger.error(msg="send email error message_id=" + str(user_message.message_id) + "reason:" + str(res.data),
                     exc_info=True)


def get_email_content(userMessage):
    content = json.loads(userMessage.message.content)
    try:
        template = loader.get_template(get_email_template(content['resource_type']))
        content['detail'] = json.loads(content['detail'])
        content['title'] = userMessage.message.title
        content['date'] = userMessage.message.date_created.strftime("%Y-%m-%d %H:%M:%S")
        email_content = template.render(content)
        return email_content
    except Exception as e:
        logger.error(msg="get email content error", exc_info=True)
        return ''


def get_email_template(type):
    templates = {
        "CLUSTER": "cluster.html",
        "CLUSTER_EVENT": "cluster-event.html",
    }
    return templates[type]


def send_ding_talk_msg(message_id):
    user_message = UserMessage.objects.get(message_id=message_id, send_type=UserMessage.MESSAGE_SEND_TYPE_DINGTALK)
    setting_dingTalk = Setting.get_settings("dingTalk")
    ding_talk = DingTalk(webhook=setting_dingTalk['DINGTALK_WEBHOOK'], secret=setting_dingTalk['DINGTALK_SECRET'])
    res = ding_talk.send_markdown_msg(receivers=user_message.receive.split(','), content=get_msg_content(user_message))
    if res.success:
        user_message.receive_status = UserMessage.MESSAGE_RECEIVE_STATUS_SUCCESS
        user_message.save()
    else:
        logger.error(msg="send dingtalk error message_id=" + str(user_message.message_id) + "reason:" + str(res.data),
                     exc_info=True)

def get_msg_content(userMessage):
    content = json.loads(userMessage.message.content)
    type = content['resource_type']
    content['detail'] = json.loads(content['detail'])
    text = ''
    if type == 'CLUSTER_EVENT':
        text = "### " + userMessage.message.title + "\n - 项目:" + content['item_name'] + \
               "\n - 集群:" + content['resource_name'] + \
               "\n- 名称:" + content['detail']['name'] + \
               "\n- 类别:" + content['detail']['type'] + \
               "\n- 原因:" + content['detail']['reason'] + \
               "\n- 组件:" + content['detail']['component'] + \
               "\n- NameSpace:" + content['detail']['namespace'] + \
               "\n- 主机:" + content['detail']['host'] + \
               "\n- 告警时间:" + content['detail']['last_timestamp'] + \
               "\n- 详情:" + content['detail']['message']+\
               "本消息由KubeOperator自动发送"

    if type == 'CLUSTER':
        text = "### " + userMessage.message.title + "\n - 项目:" + content['item_name'] + \
               "\n - 集群:" + content['resource_name'] + \
               "\n - 信息:" + content['detail']['message'] + \
               "本消息由KubeOperator自动发送"
    return {"title":userMessage.message.title, "text": text}


class MessageReceiver():

    def __init__(self, user_id, receive, send_type):
        self.user_id = user_id
        self.receive = receive
        self.send_type = send_type
