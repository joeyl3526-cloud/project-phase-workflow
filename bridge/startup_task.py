"""Reviewable, current-user, interactive-logon Task Scheduler definition."""
from pathlib import Path
from datetime import datetime, timedelta
import re
import subprocess
import xml.etree.ElementTree as ET

NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
ET.register_namespace('', NS)


def current_sid():
    result = subprocess.run(['whoami.exe', '/user', '/fo', 'csv', '/nh'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=10)
    sid = re.search(r'S-1-(?:\d+-)+\d+', result.stdout)
    if result.returncode or not sid:
        raise RuntimeError('Cannot identify current Windows user for logon startup')
    return sid.group(0)


def add_recovery_trigger(task, start_at=None):
    """One scheduler-owned recovery attempt per minute; no extra resident app."""
    triggers=task.find('{'+NS+'}Triggers')
    existing=triggers.find('{'+NS+'}TimeTrigger[@id="PhaseWorkflowRecovery"]')
    if existing is not None:
        return
    trigger=ET.SubElement(triggers,'{'+NS+'}TimeTrigger',id='PhaseWorkflowRecovery')
    repetition=ET.SubElement(trigger,'{'+NS+'}Repetition')
    ET.SubElement(repetition,'{'+NS+'}Interval').text='PT1M'
    ET.SubElement(repetition,'{'+NS+'}StopAtDurationEnd').text='false'
    ET.SubElement(trigger,'{'+NS+'}StartBoundary').text=(start_at or
        (datetime.now().astimezone()+timedelta(seconds=60)).isoformat(timespec='seconds'))
    ET.SubElement(trigger,'{'+NS+'}Enabled').text='true'


def task_xml(sid, python_exe, bridge_script, config_path):
    if not re.fullmatch(r'S-1-(?:\d+-)+\d+', sid):
        raise ValueError('Use a verified current-user SID')
    task = ET.Element('{' + NS + '}Task', version='1.4')
    def add(parent, name, value=None, **attributes):
        node = ET.SubElement(parent, '{' + NS + '}' + name, attributes)
        if value is not None:
            node.text = str(value)
        return node
    registration = add(task, 'RegistrationInfo')
    add(registration, 'Description', 'Current-user Python phase-workflow coordinator. No idle model calls.')
    triggers = add(task, 'Triggers')
    trigger = add(triggers, 'LogonTrigger')
    add(trigger, 'Enabled', 'true'); add(trigger, 'UserId', sid); add(trigger, 'Delay', 'PT10S')
    add_recovery_trigger(task)
    principals = add(task, 'Principals')
    principal = add(principals, 'Principal', id='CurrentUser')
    add(principal, 'UserId', sid); add(principal, 'LogonType', 'InteractiveToken')
    add(principal, 'RunLevel', 'LeastPrivilege')
    settings = add(task, 'Settings')
    values = {'MultipleInstancesPolicy': 'IgnoreNew', 'DisallowStartIfOnBatteries': 'false',
        'StopIfGoingOnBatteries': 'false', 'AllowHardTerminate': 'true', 'StartWhenAvailable': 'true',
        'RunOnlyIfNetworkAvailable': 'false', 'AllowStartOnDemand': 'true', 'Enabled': 'true',
        'Hidden': 'true', 'ExecutionTimeLimit': 'PT0S'}
    for key, value in values.items():
        add(settings, key, value)
    restart = add(settings, 'RestartOnFailure')
    add(restart, 'Interval', 'PT1M'); add(restart, 'Count', '3')
    actions = add(task, 'Actions', Context='CurrentUser')
    action = add(actions, 'Exec')
    add(action, 'Command', str(python_exe))
    add(action, 'Arguments', subprocess.list2cmdline(['-B', '-X', 'utf8', str(bridge_script),
                                                    '--config', str(config_path), 'coordinator']))
    add(action, 'WorkingDirectory', str(Path(bridge_script).parent))
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(task, encoding='unicode')


def task_name(sid):
    return 'PhaseWorkflow-PythonBridge-' + sid.rsplit('-', 1)[-1]
