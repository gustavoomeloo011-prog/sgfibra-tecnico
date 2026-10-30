"""Local-only visual fixture: python tests/map_preview.py (no SGP requests)."""
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'static'
TECHS = [{"id": "37", "name": "Técnico A"}, {"id": "68", "name": "Técnico B"}]
ITEMS = [{"order_id": 101, "client": "Cliente de teste A", "address": "Bairro Centro", "reason": "CORRETIVA", "technician": "Sem técnico", "scheduled_at": "25/09/2026 11:00", "position_available": True, "latitude": -23.55, "longitude": -46.63},
         {"order_id": 102, "client": "Cliente de teste B", "address": "Bairro Norte", "reason": "INSTALAÇÃO DE KIT", "technician": "Técnico B", "scheduled_at": "25/09/2026 12:00", "position_available": True, "latitude": -23.56, "longitude": -46.64}]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def reply(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/api/me':
            return self.reply({"mode": "superadmin", "nome": "Teste local", "usuario": "fixture"})
        if path == '/api/office/open-service-map':
            return self.reply({"date": "2026-09-25", "items": ITEMS, "total": 2, "located": 2})
        if path == '/api/superadmin/route-monitor':
            return self.reply({"date": "2026-09-25", "generated_at": "2026-09-25T12:00:00-03:00", "technicians": [], "alerts": [], "map_points": [
                {"technician": t['name'], "position_available": True, "latitude": -23.54 - i * .02, "longitude": -46.63, "state": "Disponível", "client": "Último serviço de teste"} for i, t in enumerate(TECHS)]})
        if path == '/api/office/itinerary':
            def stop(id, lat, lng, status, time):
                return {'id':id,'client':'Cliente teste '+str(id),'reason':'CORRETIVA','status':status,
                        'status_text':'Encerrada' if status==1 else 'Aberta','scheduled_at':'2026-09-25T'+time,
                        'finished_at':'2026-09-25T'+time if status==1 else None,
                        'position_available':True,'latitude':lat,'longitude':lng}
            return self.reply({'completed':[stop(90,-23.53,-46.62,1,'08:00:00'),stop(91,-23.54,-46.63,1,'09:00:00')],
                               'current':[], 'upcoming':[stop(101,-23.55,-46.63,0,'11:00:00'),stop(102,-23.56,-46.64,0,'12:00:00')],
                               'missing_coordinates':0})
        if path.endswith('/assignment'):
            return self.reply({"technicians": TECHS, "technician_id": "", "revision": "fixture", "scheduled_at": "25/09/2026 11:00"})
        if path.startswith('/api/'):
            return self.reply({})
        return super().do_GET()

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        if self.path.endswith('/assignment'):
            technician = next(t for t in TECHS if t['id'] == str(payload['technician_id']))
            order = int(self.path.split('/')[4])
            for item in ITEMS:
                if item['order_id'] == order:
                    item['technician'] = technician['name']
            return self.reply({"confirmed": True, "technician": technician['name']})
        self.send_error(404)


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 8769), Handler).serve_forever()
