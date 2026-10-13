"""Installed only on the dedicated Windows host, never during development."""
import os
import sys
import logging
import socket
from pathlib import Path
import win32event
import win32service
import win32serviceutil
import uvicorn
from .config import initialize_config
from .store import Store
from .web import create_app
from .deployment import service_settings

ROOT=Path(__file__).resolve().parent.parent
SETTINGS=service_settings(ROOT)


class MailService(win32serviceutil.ServiceFramework):
    _svc_name_=SETTINGS['service_name']
    _svc_display_name_='Oneasy 邮件检测与提醒助手' + ('' if SETTINGS['service_name']=='OneasyMailAssistant' else ' ('+SETTINGS['service_name']+')')
    _svc_description_='监控已启用的阿里邮箱新来信，并向负责人邮箱发送提醒与复核汇总。'
    _svc_reg_class_='app.windows_service.MailService'

    def __init__(self,args):
        self.stop_requested=False
        super().__init__(args)

    def SvcStop(self):
        self.stop_requested=True
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if hasattr(self,'server'): self.server.should_exit=True

    def SvcDoRun(self):
        os.chdir(ROOT)
        (ROOT/'logs').mkdir(exist_ok=True)
        logging.basicConfig(filename=ROOT/'logs'/'service.log',level=logging.WARNING,
                            format='%(asctime)s %(levelname)s %(message)s')
        try:
            # Bind before starting the worker: an occupied port must not create a second monitor.
            with socket.socket() as listener:
                listener.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                listener.bind(('127.0.0.1',SETTINGS['port']))
                listener.listen(128)
                config=initialize_config(ROOT/'data'/'config.json')
                self.server=uvicorn.Server(uvicorn.Config(create_app(Store(ROOT/'data'/'main.db'),config,True,desktop_control=True,desktop_port=SETTINGS['port']),
                    host='127.0.0.1',port=SETTINGS['port'],access_log=False,log_level='warning',log_config=None))
                if self.stop_requested:
                    self.server.should_exit=True
                    return
                self.server.run(sockets=[listener])
        except Exception:
            logging.exception('Oneasy service could not run')
            raise


if __name__=='__main__':
    win32serviceutil.HandleCommandLine(MailService, serviceClassString='app.windows_service.MailService')
