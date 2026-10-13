import importlib
import importlib.util
import json
import traceback
import unittest
from unittest.mock import patch

import httpx


WEBHOOK = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=unit-test-secret'
CONFIG = {'enabled': True, 'webhook': WEBHOOK}


class WeComTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('app.wecom'), 'WeCom transport is missing')
        self.wecom = importlib.import_module('app.wecom')
        self.requests = []

    def sender(self, handler=None, config=None):
        def reply(request):
            self.requests.append(request)
            if handler:
                return handler(request)
            return httpx.Response(200, json={'errcode': 0, 'errmsg': 'ok'})
        client = httpx.Client(transport=httpx.MockTransport(reply), follow_redirects=True)
        self.addCleanup(client.close)
        return self.wecom.WeComSender(CONFIG if config is None else config, client=client)

    def test_configuration_defaults_and_valid_official_webhooks(self):
        self.assertEqual(self.wecom.validate_config({}), {'enabled': False, 'webhook': ''})
        for url in (WEBHOOK, WEBHOOK.replace('.com/', '.com:443/'),
                    'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=' + 'A' * 200):
            with self.subTest(url=url):
                self.assertEqual(self.wecom.validate_config({'enabled': True, 'webhook': url}),
                                 {'enabled': True, 'webhook': url})

    def test_blank_and_absent_webhook_preserve_saved_secret(self):
        for data in ({}, {'webhook': ''}, {'webhook': ' \t\r\n'}, {'enabled': False, 'webhook': ''}):
            with self.subTest(data=data):
                result = self.wecom.validate_config(data, CONFIG)
                self.assertEqual(result['webhook'], WEBHOOK)
                self.assertEqual(result['enabled'], data.get('enabled', True))
                self.assertEqual(CONFIG, {'enabled': True, 'webhook': WEBHOOK})
        with self.assertRaises(ValueError):
            self.wecom.validate_config({'enabled': True})

    def test_configuration_rejects_unexpected_fields_and_types_safely(self):
        for value in (None, [], '', {'enabled': 1}, {'enabled': 'true'}, {'webhook': None},
                      {'webhook': []}, {'secret': WEBHOOK}):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                self.wecom.validate_config(value)
            self.assertNotIn('unit-test-secret', str(caught.exception))
            self.assertNotIn('https://', str(caught.exception))

    def test_configuration_rejects_unofficial_or_ambiguous_urls(self):
        invalid = [
            WEBHOOK.replace('https:', 'http:'),
            WEBHOOK.replace('qyapi.weixin.qq.com', 'evil.example'),
            WEBHOOK.replace('qyapi.weixin.qq.com', 'qyapi.weixin.qq.com.evil.example'),
            WEBHOOK.replace('qyapi.weixin.qq.com', 'user:pass@qyapi.weixin.qq.com'),
            WEBHOOK.replace('.com/', '.com:444/'),
            WEBHOOK.replace('/send?', '/send/?'),
            WEBHOOK.replace('/cgi-bin/', '/other/'),
            WEBHOOK + '#fragment', WEBHOOK + '&x=1', WEBHOOK + '&key=other',
            WEBHOOK.replace('key=', 'KEY='), WEBHOOK.replace('key=', 'key=%2F'),
            WEBHOOK.replace('unit-test-secret', ''),
            WEBHOOK.replace('unit-test-secret', 'A' * 201),
            WEBHOOK.replace('unit-test-secret', '含中文'),
            WEBHOOK.replace('unit-test-secret', '@all'),
            WEBHOOK.replace('unit-test-secret', 'abc/def'),
            WEBHOOK + '\n', WEBHOOK.replace('qyapi', 'qy\tapi'),
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                self.wecom.validate_config({'enabled': True, 'webhook': url})
            self.assertNotIn('https://', str(caught.exception))
            self.assertNotIn('unit-test-secret', str(caught.exception))

    def test_one_text_message_mentions_only_explicit_members(self):
        sender = self.sender()
        sender.send_text('客户开通咨询', ['LiHan', 'User.2', 'LiHan'], identity='mail-123')
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(str(request.url), WEBHOOK)
        payload = json.loads(request.content)
        self.assertEqual(payload['msgtype'], 'text')
        self.assertEqual(payload['text']['mentioned_list'], ['LiHan', 'User.2'])
        self.assertIn('客户开通咨询', payload['text']['content'])
        self.assertNotIn('mentioned_mobile_list', payload['text'])
        self.assertTrue(all(0 < seconds <= 20 for seconds in request.extensions['timeout'].values()))

    def test_long_unicode_content_fits_utf8_limit_and_points_to_full_message(self):
        sender = self.sender()
        sender.send_text('客户🙂' * 1000, ['LiHan'], identity='private-identity')
        content = json.loads(self.requests[0].content)['text']['content']
        self.assertLessEqual(len(content.encode('utf-8')), 2048)
        self.assertTrue(content.startswith('客户🙂'))
        self.assertIn('内容较长，请登录原邮箱或邮件助手查看完整信息。', content)
        self.assertNotIn('\ufffd', content)
        self.assertNotIn('private-identity', content)

    def test_content_with_exact_limit_does_not_get_truncated(self):
        self.sender().send_text('中' * 682 + 'AB', ['LiHan'])
        content = json.loads(self.requests[0].content)['text']['content']
        self.assertEqual(content, '中' * 682 + 'AB')

    def test_rejected_mentions_never_send(self):
        sender = self.sender()
        for members in ([], None, 'LiHan', ['@all'], ['ALL'], ['all'], ['@All'],
                        ['LiHan', '@all'], ['a b'], ['含中文'], ['a' * 65], [1], ['']):
            with self.subTest(members=members), self.assertRaises(self.wecom.WeComError) as caught:
                sender.send_text('消息', members)
            self.assertEqual(caught.exception.code, 'rejected')
        self.assertEqual(self.requests, [])

    def test_disabled_sender_and_invalid_content_never_send(self):
        sender = self.sender(config={'enabled': False, 'webhook': WEBHOOK})
        with self.assertRaises(self.wecom.WeComError) as caught:
            sender.send_text('消息', ['LiHan'])
        self.assertEqual(caught.exception.code, 'rejected')
        sender = self.sender()
        for value in (None, 1, '\ud800'):
            with self.subTest(value=value), self.assertRaises(self.wecom.WeComError) as caught:
                sender.send_text(value, ['LiHan'])
            self.assertEqual(caught.exception.code, 'rejected')
        self.assertEqual(self.requests, [])

    def test_http_success_with_wecom_error_is_rejected_safely(self):
        sender = self.sender(lambda request: httpx.Response(200, json={
            'errcode': 93000, 'errmsg': 'private unit-test-secret ' + WEBHOOK}))
        with self.assertRaises(self.wecom.WeComError) as caught:
            sender.send_text('消息', ['LiHan'])
        self.assertEqual(caught.exception.code, 'rejected')
        self.assertNotIn('unit-test-secret', str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    def test_malformed_responses_are_not_success(self):
        responses = [httpx.Response(200, text='private ' + WEBHOOK),
                     httpx.Response(200, json=[]), httpx.Response(200, json={}),
                     httpx.Response(200, json={'errcode': '0'}),
                     httpx.Response(200, json={'errcode': False}),
                     httpx.Response(200, json={'errcode': 0.0})]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(self.wecom.WeComError) as caught:
                self.sender(lambda request: response).send_text('消息', ['LiHan'])
            self.assertEqual(caught.exception.code, 'response')
            self.assertNotIn('unit-test-secret', str(caught.exception))

    def test_http_error_and_redirect_are_not_retried_or_followed(self):
        for status in (302, 400, 500):
            self.requests.clear()
            sender = self.sender(lambda request: httpx.Response(status,
                headers={'Location': 'https://evil.example'}, json={'errcode': 0}))
            with self.subTest(status=status), self.assertRaises(self.wecom.WeComError) as caught:
                sender.send_text('消息', ['LiHan'])
            self.assertEqual(caught.exception.code, 'response')
            self.assertEqual(len(self.requests), 1)

    def test_transport_failure_has_no_secret_in_exception_or_traceback(self):
        def fail(request):
            raise httpx.ConnectError('private ' + WEBHOOK, request=request)
        sender = self.sender(fail)
        with self.assertRaises(self.wecom.WeComError) as caught:
            sender.send_text('消息', ['LiHan'])
        self.assertEqual(caught.exception.code, 'transport')
        detail = ''.join(traceback.format_exception(caught.exception))
        self.assertNotIn('unit-test-secret', detail)
        self.assertNotIn('https://', detail)
        self.assertEqual(len(self.requests), 1)

    def test_default_client_uses_tls_without_environment_proxies_or_redirects(self):
        real_client = httpx.Client
        options = []
        def create_client(**kwargs):
            options.append(kwargs.copy())
            kwargs['transport'] = httpx.MockTransport(lambda request: httpx.Response(
                200, json={'errcode': 0, 'errmsg': 'ok'}))
            return real_client(**kwargs)
        with patch('app.wecom.httpx.Client', side_effect=create_client):
            self.wecom.WeComSender(CONFIG).send_text('消息', ['LiHan'])
        self.assertEqual(len(options), 1)
        self.assertIs(options[0]['verify'], True)
        self.assertIs(options[0]['follow_redirects'], False)
        self.assertIs(options[0]['trust_env'], False)
        self.assertLessEqual(options[0]['timeout'], 20)


if __name__ == '__main__':
    unittest.main()
