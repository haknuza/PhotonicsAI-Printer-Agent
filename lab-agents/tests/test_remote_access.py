import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app import create_app
from printing_agent.agent import PrintingAgent
from remote_access import remote_password


PASSWORD='test-password-123456'


def auth(password=PASSWORD,username='lab'):
    return {'Authorization':'Basic '+base64.b64encode(f'{username}:{password}'.encode()).decode()}


class RemoteAccess(unittest.TestCase):
    def setUp(self):
        self.agent=PrintingAgent()
        self.addCleanup(self.agent.close)
        self.app=create_app(self.agent,PASSWORD)
        self.client=self.app.test_client()

    def phone(self,path='/',headers=None,**kwargs):
        return self.client.get(path,base_url='http://192.168.10.50:8770',
                               environ_overrides={'REMOTE_ADDR':'192.168.10.51'},headers=headers,**kwargs)

    def test_remote_gate_covers_dashboard_api_and_assets(self):
        local=self.client.get('/')
        self.assertEqual(local.status_code,200)
        self.assertNotIn(b'lan-compat.js',local.data)
        for path in ('/','/api/config','/api/printer/status','/static/ui.js','/static/photonicsai-logo.png'):
            response=self.phone(path)
            self.assertEqual(response.status_code,401,path)
            self.assertIn('Basic',response.headers['WWW-Authenticate'])
            self.assertEqual(response.headers['Cache-Control'],'no-store')
        response=self.phone(headers=auth())
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data,local.data)
        self.assertEqual(self.phone('/api/config',headers=auth()).status_code,200)

    def test_wrong_credentials_host_and_forwarded_headers_cannot_bypass(self):
        for headers in (auth('wrong'),auth(username='wrong'),{'X-Forwarded-For':'127.0.0.1'},
                        {'Host':'localhost:8770'},{'Authorization':'Bearer anything'}):
            self.assertEqual(self.phone(headers=headers).status_code,401)
        # A local peer opening a LAN Host also needs the password.
        self.assertEqual(self.client.get('/',base_url='http://192.168.10.50:8770').status_code,401)

    def test_removed_control_routes_still_require_remote_authentication(self):
        # Auth runs even before routing rejects a removed control endpoint.
        for path in ('/api/prepare','/api/stop','/api/runs/test/execute'):
            response=self.client.post(path,json={},base_url='http://192.168.10.50:8770',
                                      environ_overrides={'REMOTE_ADDR':'192.168.10.51'},headers={'X-Lab-Control':'local'})
            self.assertEqual(response.status_code,401)
        response=self.client.post('/api/stop',json={},base_url='http://192.168.10.50:8770',
                                  environ_overrides={'REMOTE_ADDR':'192.168.10.51'},headers=auth())
        self.assertEqual(response.status_code,404)

    def test_unconfigured_remote_access_is_closed(self):
        with patch.dict(os.environ,{},clear=True):
            app=create_app(self.agent)
            with app.test_client() as client:
                self.assertEqual(client.get('/api/config',environ_overrides={'REMOTE_ADDR':'192.168.10.51'}).status_code,503)
                self.assertEqual(client.get('/api/config').status_code,200)

    def test_generated_password_is_private_persistent_and_environment_override_works(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=True):
            password,path=remote_password(tmp)
            self.assertGreaterEqual(len(password),12)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(remote_password(tmp)[0],password)
            os.environ['LAB_REMOTE_PASSWORD']=PASSWORD
            self.assertEqual(remote_password(tmp),(PASSWORD,None))
            os.environ['LAB_REMOTE_PASSWORD']='short'
            with self.assertRaises(ValueError):remote_password(tmp)
