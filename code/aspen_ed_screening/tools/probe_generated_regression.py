"""Test a generated archive in a fresh Aspen Properties COM document.

Does not open any template or attach to a user's active simulation. Results
remain blocked until a separate, independent validation completes.
"""
import argparse
import json
import time
import sys
from pathlib import Path


def run(archive, output):
    import pythoncom
    import win32com.client
    report = dict(archive=str(archive), status='blocked_unvalidated', events=[], parameters={})
    app = None
    class Events:
        def OnControlPanelMessage(self, clear, message):
            report['events'].append(str(message))
        def OnDialogSuppressed(self, message, result):
            report.setdefault('dialogs', []).append(str(message))
    def values(node, path, depth=12):
        result = {}
        if node is None:
            return result
        try:
            children = list(node.Elements)
        except Exception:
            children = []
        if children and depth:
            for child in children:
                result.update(values(child, path+'\\'+str(child.Name), depth-1))
        else:
            try:
                value = node.Value
                if isinstance(value, (str, int, float, type(None))):
                    result[path] = value
            except Exception:
                pass
        return result
    output.mkdir(parents=True, exist_ok=False)
    try:
        app = win32com.client.gencache.EnsureDispatch(
            win32com.client.DispatchEx('AspenProperties.Document.37.0'))
        sink = win32com.client.WithEvents(app, Events)
        app.Visible = False
        if archive.suffix.lower() == '.inp' and report['events']:
            # Use the full V11 signature so import errors are returned to the
            # caller instead of being left in a modal dialog.
            app.InitFromFile2(str(archive.resolve()), False, 0, '', '', '', str(output.resolve()))
        else:
            app.InitFromArchive2(str(archive.resolve()), 0, '', '', '', str(output.resolve()))
        if archive.suffix.lower() == '.inp':
            start = time.monotonic()
            while app.Engine.IsRunning:
                pythoncom.PumpWaitingMessages()
                if time.monotonic() - start > 120:
                    app.Engine.Stop()
                    raise TimeoutError('Aspen input import exceeded 120 seconds')
                time.sleep(.2)
        path = r'\Data\Properties\Regression'
        report['import_regression_tree'] = values(app.Tree.FindNode(path), path)
        if not report['import_regression_tree']:
            report['properties_tree'] = values(app.Tree.FindNode(r'\\Data\\Properties'), r'\\Data\\Properties', depth=5)
            try:
                report['root_children'] = [str(x.Name) for x in app.Tree.Elements]
            except Exception:
                report['root_children'] = []
        app.Generate(str((output/'generated.inp').resolve()), 0)
        if not report['import_regression_tree']:
            raise RuntimeError('No regression case imported')
        app.Engine.Run2(True)
        start = time.monotonic()
        while app.Engine.IsRunning:
            pythoncom.PumpWaitingMessages()
            if time.monotonic()-start > 90:
                app.Engine.Stop()
                raise TimeoutError('Aspen regression exceeded 90 seconds')
            time.sleep(.2)
        pythoncom.PumpWaitingMessages()
        report['result_tree'] = values(app.Tree.FindNode(path), path)
        report['parameters'] = {k:v for k,v in report['result_tree'].items()
                                if '\\Output\\ESTVALUE2\\' in k}
        app.Engine.ExportReport(str((output/'engine_report.txt').resolve()), 12)
    except Exception as exc:
        report['error'] = repr(exc)
    finally:
        if app is not None:
            try: app.Close(False)
            except Exception: pass
            try: app.Quit()
            except Exception: pass
        (output/'probe.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('import_regression_tree','result_tree')}, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--win32-deps', type=Path)
    args = parser.parse_args()
    if args.win32_deps:
        for sub in ('', 'win32', 'win32/lib', 'pythonwin'):
            sys.path.insert(0, str(args.win32_deps/sub))
    run(args.archive, args.output)
