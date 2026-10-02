"""Real-monitor package upgrade, failure recovery and private runtime fixtures."""
import importlib.util
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_deploy_manage as legacy

SHA_A, SHA_B, archive_bytes = legacy.SHA_A, legacy.SHA_B, legacy.archive_bytes

BASE = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('live_runtime_fixture', BASE / 'scripts/deploy/runtime.py')
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def live_archive(sha):
    return archive_bytes(sha, extra={
        'release.json': json.dumps({'schema': 1, 'kind': 'monitoring-server', 'version': '0.3.0', 'commit': sha}).encode(),
        '.deploy/console.py': b'# console\n', '.deploy/runtime.py': b'# runtime\n',
        '.backend/server.py': b'# backend\n', '.agent/agent.py': b'# agent\n', '.agent/install.sh': b'#!/bin/bash\n',
    })


class LiveUpgradeTests(unittest.TestCase):
    # Reuse the remote transport fixture without repeating its whole test suite.
    setUp = legacy.DeploymentTests.setUp
    write_config = legacy.DeploymentTests.write_config
    fetcher = legacy.DeploymentTests.fetcher
    require_symlinks = legacy.DeploymentTests.require_symlinks
    deploy = legacy.DeploymentTests.deploy
    def test_live_upgrade_runs_runtime_and_keeps_legacy_previous(self):
        self.deploy(SHA_A)
        state = Mock()
        module = Mock()
        module.api_port.return_value = 8091
        module.Activation.return_value = state
        import test_deploy_manage
        with patch.object(test_deploy_manage.manage, '_runtime', return_value=module):
            self.deploy(SHA_B, archive=live_archive(SHA_B))
        self.assertEqual([x[0] for x in state.method_calls], ['prepare', 'start'])
        self.assertEqual(self.manager.status()['kind'], 'monitoring-server')
        self.assertEqual(self.manager.status()['previous'], SHA_A)
        self.assertTrue(self.manager._config()['backendEnabled'])
        with self.assertRaisesRegex(Exception, '模拟原型'):
            self.manager.rollback()
        self.assertEqual(self.manager.status()['current'], SHA_B)

    def test_failed_backend_start_restores_site_and_frontend(self):
        self.deploy(SHA_A)
        state = Mock()
        state.start.side_effect = ValueError('backend failed')
        module = Mock()
        module.api_port.return_value = 8091
        module.Activation.return_value = state
        import test_deploy_manage
        with patch.object(test_deploy_manage.manage, '_runtime', return_value=module):
            with self.assertRaisesRegex(ValueError, 'backend failed'):
                self.deploy(SHA_B, archive=live_archive(SHA_B))
        state.restore.assert_called_once()
        self.assertEqual(self.manager.status()['current'], SHA_A)
        self.assertNotIn('backendEnabled', self.manager._config())

    def test_missing_backend_component_is_rejected_before_activation(self):
        self.require_symlinks()
        bundle = archive_bytes(SHA_A, extra={
            'release.json': json.dumps({'schema': 1, 'kind': 'monitoring-server', 'version': '0.3', 'commit': SHA_A}).encode()
        })
        with self.assertRaisesRegex(Exception, 'missing'):
            self.deploy(SHA_A, archive=bundle)
        self.assertFalse((self.root / 'current').exists())


class RuntimeConfigurationTests(unittest.TestCase):
    def recovery_fixture(self, root, started=False):
        site, proxy, service = [root / value for value in ('site.conf', 'proxy.conf', 'backend.service')]
        for path in (site, proxy, service):
            path.write_text('new configuration', encoding='utf-8')
        activation = runtime.Activation.__new__(runtime.Activation)
        activation.snapshots = {site: 'old site', proxy: 'old TLS proxy', service: None}
        activation.changed = True
        activation.start_attempted = started
        activation.units_touched = started
        activation.was_active = activation.was_enabled = False
        return activation, site, proxy, service

    def test_first_upgrade_prepare_failure_restores_nginx_without_stopping_missing_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            activation, site, proxy, service = self.recovery_fixture(Path(directory))
            with patch.object(runtime, 'SERVICE', service), patch.object(runtime, 'run') as command:
                activation.restore()
            self.assertEqual(site.read_text(), 'old site')
            self.assertEqual(proxy.read_text(), 'old TLS proxy')
            self.assertFalse(service.exists())
            self.assertFalse(any(call.args[0][:2] == ['systemctl', 'stop'] for call in command.call_args_list))

    def test_failed_stop_still_restores_all_files_and_reports_recovery_error(self):
        with tempfile.TemporaryDirectory() as directory:
            activation, site, proxy, service = self.recovery_fixture(Path(directory), started=True)
            def command(arguments):
                if arguments[:2] == ['systemctl', 'stop']:
                    raise OSError('fixture stop failed')
            with patch.object(runtime, 'SERVICE', service), patch.object(runtime, 'run', side_effect=command), self.assertRaisesRegex(ValueError, '恢复错误'):
                activation.restore()
            self.assertEqual(site.read_text(), 'old site')
            self.assertEqual(proxy.read_text(), 'old TLS proxy')
            self.assertFalse(service.exists())

    def test_inner_nginx_preserves_host_and_trusts_tls_only_from_local_outer_proxy(self):
        original = runtime.OWNER + '\n' + runtime.ROOT_PREFIX + '/opt/watch\nserver {\n listen 8088;\n root /opt/watch/current;\n}\n'
        value = runtime.nginx_site(original, 8091)
        self.assertEqual(runtime.nginx_site(value, 8091), value)
        self.assertIn('"127.0.0.1:https" https;', value)
        self.assertIn('default $scheme;', value)
        self.assertIn('proxy_set_header Host $http_host;', value)
        self.assertIn('proxy_set_header X-Forwarded-Proto $xiaowork_watch_scheme;', value)
        self.assertIn('location ^~ /api/', value)
        self.assertIn('location ^~ /agent/', value)
        self.assertEqual(value.count('map '), 2)

    def test_systemd_runs_without_root_and_writes_only_private_persistent_directory(self):
        value = runtime.unit(PurePosixPath('/opt/watch'), 8091, SHA_A)
        self.assertIn('User=xiaowork-watch', value)
        self.assertIn('ProtectSystem=strict', value)
        self.assertIn('ReadWritePaths=/opt/watch/shared/data', value)
        self.assertIn('current/.backend/server.py', value)
        self.assertIn('Environment=WATCH_RELEASE=' + SHA_A, value)
        self.assertIn('--host 127.0.0.1', value)
        self.assertIn('UMask=0077', value)

    def test_backend_port_rejects_injection_or_reserved_values(self):
        for value in ('8091; command', 80, True, 70000, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runtime.api_port({'backendPort': value})
        self.assertEqual(runtime.api_port({'healthUrl': 'http://127.0.0.1:8091/release.json'}), 8092)
        with self.assertRaisesRegex(ValueError, '网站端口'):
            runtime.api_port({'healthUrl': 'http://127.0.0.1:8091/release.json', 'backendPort': 8091})

    def test_public_domain_alias_preserves_original_inner_vhost(self):
        value = runtime.site_domains('server { server_name inner.example.com; }', ['watch.example.com'])
        self.assertIn('server_name inner.example.com watch.example.com;', value)

    def test_proxy_health_waits_for_reload_and_preserves_public_host(self):
        activation = runtime.Activation.__new__(runtime.Activation)
        activation.config = {'healthUrl': 'http://127.0.0.1:8088/release.json', 'proxyDomain': 'watch.example.com'}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = [b'<html>old worker</html>', json.dumps({'code': 0, 'data': {'release': SHA_A}}).encode()]
        opener = Mock()
        opener.open.return_value = response
        with patch.object(runtime, 'build_opener', return_value=opener), patch.object(runtime.time, 'sleep') as pause:
            activation.proxy_health(SHA_A)
        self.assertEqual(opener.open.call_count, 2)
        pause.assert_called_once_with(0.25)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8088/api/health')
        self.assertEqual(request.get_header('Host'), 'watch.example.com')

    @unittest.skipUnless(shutil.which('nginx'), 'Real Nginx config verification requires Nginx')
    def test_real_nginx_accepts_managed_api_locations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = runtime.nginx_site('server {\n listen 18088;\n root ' + root.as_posix() + ';\n}\n', 8091)
            configuration = ('pid ' + (root / 'pid').as_posix() + ';\nerror_log stderr;\nevents {}\n'
                             'http { access_log off;\n' + site + '}\n')
            path = root / 'nginx.conf'
            path.write_text(configuration, encoding='utf-8')
            result = subprocess.run(['nginx', '-t', '-p', str(root), '-c', str(path)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
