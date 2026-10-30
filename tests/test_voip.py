import ast
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from app.voip import EditFormParser, parse_inventory, refresh, reveal
from app.sgp import SGPError


def card(number='1199999000', ip='', note='', active='Ativa', password='test-only'):
    return f'<div class="card"><div class="card-body"><div>Conta:<input type="text" value="{number}">Password:<input type="password" value="{password}"><input type="text" disabled value="{ip}"><div>OBSERVACAO: {note}</div></div><div>Conta: {active}</div></div></div>'


def page(*cards):
    return f'<div>REGISTRADO: 0 NAO REGISTRADO :{len(cards)}</div>' + ''.join(cards)


class VoipTests(unittest.TestCase):
    def test_classification(self):
        for ip,note,active,expected in [('', '', 'Ativa', 'available'),('', 'VAGO', 'Ativa', 'available'),('', 'SEM USO', 'Ativa', 'available'),('1.2.3.4:5060','VAGO','Ativa','in_use'),('', 'contrato 123', 'Ativa', 'review'),('', 'Reservado', 'Ativa', 'review'),('', '', 'Desativada', 'review')]:
            with self.subTest(ip=ip,note=note):
                rows, secrets = parse_inventory(page(card(ip=ip,note=note,active=active)))
                self.assertEqual(rows[0]['status'],expected)
                self.assertNotIn('password', str(rows))
                self.assertEqual(secrets['1199999000'],'test-only')

    def test_duplicates_any_ip_wins(self):
        rows,_=parse_inventory(page(card(),card(ip='1.2.3.4:5060')))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'in_use')

    def test_conflicting_notes_or_secrets_not_available(self):
        for other in (card(note='123'),card(password='other-test')):
            rows,_=parse_inventory(page(card(),other))
            self.assertEqual(rows[0]['status'],'review')
        _, secrets= parse_inventory(page(card(),card(password='other-test')))
        self.assertEqual(secrets,{})

    def test_incomplete_or_login_page_fails_closed(self):
        for html in ('<form>Login</form>',page(card()).replace('REGISTRADO :1','REGISTRADO :2'),page(card()).replace('OBSERVACAO:', 'UNKNOWN:')):
            with self.assertRaises(SGPError): parse_inventory(html)

    def test_refresh_saves_metadata_only_and_preserves_previous_on_error(self):
        save=Mock()
        rows,secrets=parse_inventory(page(card()))
        with patch('app.voip._last_attempt',0), patch('app.voip.snapshot_key',return_value='test'),patch('app.voip.fetch_inventory',return_value=(rows,secrets)):
            refresh(save)
        self.assertNotIn('test-only',str(save.call_args))
        save.reset_mock()
        with patch('app.voip._last_attempt',0),patch('app.voip.fetch_inventory',side_effect=SGPError('invalid',502)):
            with self.assertRaises(SGPError): refresh(save)
        save.assert_not_called()

    def test_reveal_missing_or_invalid(self):
        with self.assertRaises(SGPError):reveal('../bad')
        with patch('app.voip.fetch_inventory',return_value=([],{})):
            with self.assertRaises(SGPError):reveal('1199999000')

    def test_edit_form_parser_uses_account_key_not_generated_ids(self):
        html='''<form><input name="pass" type="number" value="1234"><input name="hdlog" type="hidden" value="1199999000"><textarea name="txtobs">Contrato 10</textarea><input name="btfunc" type="submit" value="Update"></form>'''
        forms=EditFormParser(html).forms
        self.assertEqual(len(forms),1)
        self.assertEqual(dict(forms[0]['fields'])['hdlog'],'1199999000')
        self.assertEqual(dict(forms[0]['fields'])['txtobs'],'Contrato 10')


class VoipAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_voip_is_supervisor_only(self):
        tree=ast.parse(Path('app_fastapi/main.py').read_text(encoding='utf-8'))
        names={'require_superadmin_session','require_office_tools','require_voip_reader','require_voip_supervisor','voip_inventory','voip_refresh','voip_password'}
        functions=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
        self.assertEqual(len(functions),7)
        for node in functions:
            node.decorator_list=[];node.returns=None
            node.args.defaults=[ast.Constant(None) for _ in node.args.defaults]
            for arg in node.args.args:arg.annotation=None
        namespace={'SGPError':SGPError}
        async def sync(function): return function()
        namespace.update(run_sync=sync, role_for=lambda username:'office', JSONResponse=lambda value,headers=None:value,
                         cache_snapshot=lambda *args,**kwargs:{'items':[]})
        exec(compile(ast.fix_missing_locations(ast.Module(body=functions,type_ignores=[])),'permissions','exec'),namespace)
        for mode in ('technician','admin',None):
            context=('sid',{'user':{'mode':mode}},Mock())
            for name,args in [('voip_inventory',()),('voip_refresh',()),('voip_password',('1199999000',))]:
                with self.assertRaises(SGPError) as caught:await namespace[name](*args,context=context)
                self.assertEqual(caught.exception.status,403)
        office=('sid',{'user':{'mode':'office','usuario':'office'}},Mock())
        for name,args in [('voip_inventory',()),('voip_refresh',()),('voip_password',('1199999000',))]:
            with self.assertRaises(SGPError) as caught:await namespace[name](*args,context=office)
            self.assertEqual(caught.exception.status,403)


if __name__ == '__main__':unittest.main()
