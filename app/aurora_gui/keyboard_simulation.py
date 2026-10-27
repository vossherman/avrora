"""Explicit synthetic keyboard events for automatic imports, never credential input."""
import time


_SHIFTED = dict(zip('~!@#$%^&*()_+{}|:"<>?', '`1234567890-=[]\\;\',./'))
_PUNCTUATION = {
    '`': (192, 'Backquote'), '-': (189, 'Minus'), '=': (187, 'Equal'),
    '[': (219, 'BracketLeft'), ']': (221, 'BracketRight'), '\\': (220, 'Backslash'),
    ';': (186, 'Semicolon'), "'": (222, 'Quote'), ',': (188, 'Comma'),
    '.': (190, 'Period'), '/': (191, 'Slash'), ' ': (32, 'Space'),
}
_RUSSIAN = dict(zip('йцукенгшщзхъфывапролджэячсмитьбюё',
                     'qwertyuiop[]asdfghjkl;\'zxcvbnm,.' + '`'))


def _pair(key, which, code, shift=False, ctrl=False):
    for kind in ('keydown', 'keyup'):
        yield [kind, which, shift, False, False, which, key, code, ctrl, 0]


def _character(char):
    if char in ('\n', '\t'):
        yield from _pair('Enter' if char == '\n' else 'Tab',
                         13 if char == '\n' else 9, 'Enter' if char == '\n' else 'Tab')
        return
    lower = char.lower()
    shifted = char != lower or char in _SHIFTED
    physical = _RUSSIAN.get(lower, _SHIFTED.get(char, lower))
    if 'a' <= physical <= 'z' and len(physical) == 1:
        which, code = ord(physical.upper()), 'Key' + physical.upper()
    elif '0' <= physical <= '9' and len(physical) == 1:
        which, code = ord(physical), 'Digit' + physical
    else:
        # Non-keyboard Unicode uses an explicit unidentified synthetic key.
        which, code = _PUNCTUATION.get(physical, (0, 'Unidentified'))
    if shifted:
        yield ['keydown', 16, True, False, False, 16, 'Shift', 'ShiftLeft', False, 0]
    yield from _pair(char, which, code, shifted)
    if shifted:
        yield ['keyup', 16, False, False, False, 16, 'Shift', 'ShiftLeft', False, 0]


def _normalized_text(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _edit_region(old, new):
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(old), len(new)) - prefix and old[-suffix-1] == new[-suffix-1]:
        suffix += 1
    end = len(new) - suffix if suffix else len(new)
    return prefix, suffix, end


def edit_cursor(old, new):
    """Return the final cursor index after replacing the changed text region."""
    old, new = _normalized_text(old), _normalized_text(new)
    prefix, _, end = _edit_region(old, new)
    return prefix + (end - prefix)


def edit_events(old, new, cursor=None):
    """Move to the changed region, delete old text, then insert the replacement.

    A virtual cursor is positioned using Ctrl+End and arrows when needed.
    Events describe the required edit only; randomized timing is applied separately.
    Clipboard operations and unrelated activity aren't generated.
    """
    old, new = _normalized_text(old), _normalized_text(new)
    if old == new:
        return
    prefix, suffix, end = _edit_region(old, new)
    deletion_end = len(old) - suffix
    if cursor is None:
        yield ['keydown', 17, False, False, False, 17, 'Control', 'ControlLeft', True, 0]
        yield from _pair('End', 35, 'End', ctrl=True)
        yield ['keyup', 17, False, False, False, 17, 'Control', 'ControlLeft', False, 0]
        cursor = len(old)
    else:
        cursor = max(0, min(len(old), int(cursor)))
    while cursor > deletion_end:
        yield from _pair('ArrowLeft', 37, 'ArrowLeft')
        cursor -= 1
    while cursor < deletion_end:
        yield from _pair('ArrowRight', 39, 'ArrowRight')
        cursor += 1
    for _ in range(deletion_end - prefix):
        yield from _pair('Backspace', 8, 'Backspace')
    for char in new[prefix:end]:
        yield from _character(char)


def text_edits(old, new):
    """Only editable values, not JSON keys or generated HTML, become keystrokes."""
    if isinstance(new, dict):
        previous = old if isinstance(old, dict) else {}
        for key, value in new.items():
            yield from text_edits(previous.get(key), value)
    elif isinstance(new, list):
        previous = old if isinstance(old, list) else []
        for index, value in enumerate(new):
            yield from text_edits(previous[index] if index < len(previous) else None, value)
    elif isinstance(new, str) or (isinstance(new, (int, float)) and not isinstance(new, bool)):
        before = '' if old is None or isinstance(old, (list, dict, bool)) else str(old)
        after = str(new)
        if before != after:
            yield before, after


def keypress_groups(events):
    """Keep a key press or modifier chord together through its final release."""
    pressed = set()
    group = []
    for event in events:
        group.append(event)
        if event[0] == 'keydown':
            pressed.add(event[7])
        else:
            pressed.discard(event[7])
        if not pressed:
            yield group
            group = []
    if group:
        raise ValueError('Incomplete synthetic key press')


def keypress_schedule(group, start, end, rng=None):
    """Place a short, bounded gesture at a variable point in its slot."""
    import random

    rng = rng or random.Random()
    span = max(0.0, end - start)
    hold = min(rng.uniform(.035, .08), span * rng.uniform(.25, .75))
    began = start + rng.uniform(0.0, max(0.0, span - hold))
    weights = [rng.uniform(.75, 1.25) for _ in range(max(0, len(group) - 1))]
    weight_sum = sum(weights)
    elapsed_weight = 0.0
    for index, event in enumerate(group):
        if index:
            elapsed_weight += weights[index - 1]
        yield began + (hold * elapsed_weight / weight_sum if weight_sum else 0), event


class KeyboardBuffer:
    def __init__(self, sess, clock):
        self.sess, self.clock = sess, clock
        origin = getattr(sess, '_synthetic_key_origin', None)
        if origin is None:
            origin = (clock(), time.time() * 1000)
            sess._synthetic_key_origin = origin
        self.origin, self.start = origin
        self.next_flush = self.origin + int(clock() - self.origin) + 1.0
        self.rows = []
        self.pressed = {}

    def emit(self, event):
        self.rows.append([max(0, (self.clock() - self.origin) * 1000)] + event)
        code = event[7]
        if event[0] == 'keydown':
            self.pressed[code] = event
        else:
            self.pressed.pop(code, None)
        if len(self.rows) >= 4096:
            self.flush()  # Bound packet memory for very short, large imports.

    def flush(self):
        if self.rows:
            rows, self.rows = self.rows, []
            self.sess.send('sendKBE', {'start': self.start, 'list': rows})

    def tick(self):
        now = self.clock()
        if now >= self.next_flush:
            self.flush()
            self.next_flush += (int((now - self.next_flush) // 1) + 1)

    def finish(self):
        # Cancellation may interrupt a key pair. Release pending keys before flushing.
        for code in sorted(list(self.pressed), key=lambda c: c in ('ShiftLeft', 'ControlLeft')):
            event = list(self.pressed[code])
            event[0] = 'keyup'
            event[2] = 'ShiftLeft' in self.pressed and code != 'ShiftLeft'
            event[8] = 'ControlLeft' in self.pressed and code != 'ControlLeft'
            self.emit(event)
        self.flush()
