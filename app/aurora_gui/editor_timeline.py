"""Virtual editor text/cursors and change-triggered CodeMirror-style lint debounce."""
import math


def normalized(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


class EditorTimeline:
    def __init__(self, sess, task_id, files, clock, enabled=True):
        self.sess, self.task_id, self.clock = sess, task_id, clock
        self.files = dict(files or [])
        self.cursors = {}
        self.pending = {}
        self.active = None
        self.enabled = enabled

    def activate(self, name):
        if name not in self.cursors:
            self.files[name] = normalized(self.files.get(name, ''))
            self.cursors[name] = 0  # A newly opened CodeMirror document starts at its beginning.
        if self.active != name:
            self.active = name
            self.request(name)  # Original explicitly performs lint on tab activation.

    def request(self, name):
        if self.enabled:
            self.sess.send('getCodeLint', {'idTask': int(self.task_id), 'fileName': name,
                                         'files': [[n, text] for n, text in self.files.items()]})

    def changed(self, name, text):
        if text != self.files.get(name, ''):
            self.files[name] = text
            if self.enabled:
                self.pending[name] = self.clock() + .5

    def apply_event(self, name, event):
        if event[0] != 'keydown':
            return
        text = self.files[name]
        cursor = self.cursors.get(name, 0)
        key, ctrl = event[6], event[8]
        if key == 'End' and ctrl:
            cursor = len(text)
        elif key == 'ArrowLeft':
            cursor = max(0, cursor - 1)
        elif key == 'ArrowRight':
            cursor = min(len(text), cursor + 1)
        elif key == 'Backspace':
            if cursor:
                text = text[:cursor - 1] + text[cursor:]
                cursor -= 1
        elif key in ('Enter', 'Tab') or (len(key) == 1 and not ctrl):
            value = {'Enter': '\n', 'Tab': '\t'}.get(key, key)
            text = text[:cursor] + value + text[cursor:]
            cursor += len(value)
        self.cursors[name] = cursor
        self.changed(name, text)

    def next_due(self):
        return min(self.pending.values(), default=math.inf)

    def tick(self):
        now = self.clock()
        due = sorted((deadline, name) for name, deadline in self.pending.items()
                     if deadline <= now + 1e-9)
        for _, name in due:
            del self.pending[name]
            self.request(name)
