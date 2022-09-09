import sys
import datetime
import os

IS_WIN = os.name=='nt'
IS_MAC = sys.platform=='darwin'

from time import sleep, time
from subprocess import Popen, PIPE, STDOUT, call
if IS_WIN:
    from subprocess import STARTUPINFO, STARTF_USESHOWWINDOW
from threading import Thread, Lock
from collections import namedtuple
import json
import shlex

if not IS_WIN:
    import pty # Pseudo terminal utilities
    import signal

import cudatext_keys as keys
import cudatext_cmd as cmds
from cudax_lib import html_color_to_int, int_to_html_color
from cudatext import *

from cudax_lib import get_translation
_ = get_translation(__file__)  # I18N

from .mcolor import MColor
from .pyte import *

fn_icon = os.path.join(os.path.dirname(__file__), 'terminal.png')
fn_config = os.path.join(app_path(APP_DIR_SETTINGS), 'cuda_terminal_plus.ini')
fn_history = os.path.join(app_path(APP_DIR_SETTINGS), 'cuda_terminal_plus_history.txt')
fn_state = os.path.join(app_path(APP_DIR_SETTINGS), 'cuda_terminal_plus_state.json')

fn_icon_pluss = os.path.join(os.path.dirname(__file__), 'cuda_pluss.png')
fn_icon_cross = os.path.join(os.path.dirname(__file__), 'cuda_cross.png')

ICON_FOLDERS = [
    os.path.join(os.path.dirname(__file__), 'terminalicons'),
    os.path.join(app_path(APP_DIR_DATA), 'terminalicons'), #TODO test
]

MAX_BUFFER = 100*1000
IS_UNIX_ROOT = not IS_WIN and os.geteuid()==0
SHELL_UNIX = 'bash'
SHELL_MAC = 'bash'
SHELL_WIN = 'cmd.exe'
ENC = 'cp866' if IS_WIN else 'utf8'
BASH_CHAR = '#' if IS_UNIX_ROOT else '$'
BASH_PROMPT = 'echo [$USER:$PWD]'+BASH_CHAR+' '
BASH_CLEAR = 'clear'
CMD_CLEAR = 'cls'
MSG_ENDED = _("\nConsole process was terminated.\n")
READSIZE = 4*1024
INPUT_H = 26
TERMBAR_H = 20
HOMEDIR = os.path.expanduser('~') # should work on Windows

ColorRange = namedtuple('ColorRange', 'start length fgcol bgcol isbold')
DEFAULT_FGCOL = 'default' # 37
DEFAULT_BGCOL = 'default' # 40
MAX_TERM_NAME_LEN = 12
SHELL_START_DIR = 'file' # file,project,user
HISTORY_GLOBAL_TAIL_LEN = 10 # when terminal local history is disabled - show this many from global
ZEBRA_LIGHTNESS_DELTA = 8 # percent
TERM_WRAP = 'char' # off,char,word,(int)
TERM_HEIGHT = 24
LOCK_H_SCROLL = False

SHELL_THEME_FG = '#2e3436,#cc0000,#4e9a06,#c4a000,#3465a4,#75507b,#06989a,#d3d7cf,#555753,#ef2929,#8ae234,#fce94f,#729fcf,#ad7fa8,#34e2e2,#eeeeec,#d3d7cf'
SHELL_THEME_BG = '#2e3436,#cc0000,#4e9a06,#c4a000,#3465a4,#75507b,#06989a,#d3d7cf,#555753,#ef2929,#8ae234,#fce94f,#729fcf,#ad7fa8,#34e2e2,#eeeeec,#300a24'


CMD_CLOSE_LAST_CUR_FILE = 101
CMD_CLOSE = 102 # vargs: 'ind'=index of terminal to close; otherwise - close active terminal
CMD_CUR_FILE_TERM_SWITCH = 104
CMD_NEXT = 105
CMD_PREVIOUS = 106
CMD_EXEC_SEL = 107
CMD_RENAME = 108 # vargs: 'ind'=index of terminal to rename; otherwise - close active terminal; 'newname'

#TERM_KEY_UP = b'\x1B\x4f\x41'
TERM_KEY_DOWN = b'\x1B\x4f\x42'
#TERM_KEY_PAGE_UP = b'\x1B\x5B\x35\x7E'
TERM_KEY_PAGE_DOWN = b'\x1B\x5B\x36\x7E'

history = []

cb_fs = 'module=cuda_terminal_plus;cmd={cmd};'
cbi_fs = 'module=cuda_terminal_plus;cmd={cmd};info={info};'

#TODO apply theme on change

#TODO windows
#TODO check config validity on load?
#TODO test expanduser() on win -- works like a charm (fm)


# search works very fast on million of 100char strings
def add_to_history(toadd, maxlen):
    """ adds str or list of str to history"""
    if type(toadd) == str:
        if toadd in history:
            history.remove(toadd)
        history.append(toadd)
    else: # batch add (initial load)
        history.extend(toadd)

    if len(history) > maxlen*1.1:
        del history[:maxlen]

def log(s):
    # Change conditional to True to log messages in a Debug process
    if False:
        now = datetime.datetime.now()
        print(now.strftime("%H:%M:%S ") + s)
    pass

def pretty_path(path):
    if path.startswith(HOMEDIR):
        return path.replace(HOMEDIR, '~', 1)
    return path

def bool_to_str(v):
    return '1' if v else '0'

def str_to_bool(s):
    return s=='1'

def activate_bottompanel(name):
    log(' [activating panel: <' + str(name) + '>]')
    app_proc(PROC_BOTTOMPANEL_ACTIVATE, name)

class ControlTh(Thread):
    def __init__(self, Cmd):
        Thread.__init__(self)
        self.Cmd = Cmd

    def add_buf(self, s, clear):
        self.Cmd.block.acquire()

        ### main thread is stopped here
        self.Cmd.btextchanged = True
        # limit the buffer size!
        self.Cmd.btext = (self.Cmd.btext+s)[-MAX_BUFFER:]
        if clear:

            if self.Cmd.ch_out and self.Cmd.ch_pid > 0:
                try:
                    os.waitpid(self.Cmd.ch_pid, os.WNOHANG) # check if current child terminal process exists
                except ChildProcessError:
                    # child process is gone, close stuff  (terminal exited by itself)
                    ch_out = self.Cmd.ch_out
                    self.Cmd.ch_out = None
                    self.Cmd.ch_pid = -1

                    if ch_out:
                        ch_out.close()
                else:
                    # child exists, continue reading  (shell process got restarted by Terminal())
                    pass

            self.Cmd.p=None
        self.Cmd.block.release()

    def run(self):
        if not IS_WIN:
            while True:
                if self.Cmd.stop_t: return
                if not self.Cmd.ch_out:
                    sleep(0.5)
                    continue
                try:
                    s = self.Cmd.ch_out.read(READSIZE)
                except OSError:
                    s = MSG_ENDED.encode(ENC)
                    self.add_buf(s, clear=True)
                    # don't break, shell will be restarted
                else: # no exception
                    if s != '':
                        self.add_buf(s, clear=False)

        else:
            while True:
                if self.Cmd.stop_t: return
                if not self.Cmd.p:
                    sleep(0.5)
                    continue
                pp1 = self.Cmd.p.stdout.tell()
                self.Cmd.p.stdout.seek(0, 2)
                pp2 = self.Cmd.p.stdout.tell()
                self.Cmd.p.stdout.seek(pp1)
                if self.Cmd.p.poll() is not None:
                    s = MSG_ENDED.encode(ENC)
                    self.add_buf(s, True)
                    # don't break, shell will be restarted
                elif pp2!=pp1:
                    s = self.Cmd.p.stdout.read(pp2-pp1)
                    self.add_buf(s, False)
                sleep(0.02)

class Terminal:
    memo_count = 0

    def __init__(self, h_dlg, filepath, shell, font_size, max_history, colmapfg, colmapbg, state=None):
        self.h_dlg = h_dlg

        if state: # from state
            self.name = state.get('name', '')
            self.filepath = os.path.expanduser(state.get('filepath', ''))
            self.cwd = os.path.expanduser(state.get('cwd', ''))
            self.lastactive = state.get('lastactive')
            self.icon = state.get('icon', '')
            self.wrap = state.get('wrap')
            if max_history > 0:
                self.history = state.get('history', [])[-max_history:]
            else:
                self.history = []
        else: # new
            if filepath:
                filepath = os.path.expanduser(filepath)
                self.filepath = filepath
                self.name = os.path.split(filepath)[1]
                self.cwd = self._get_file_start_dir(filepath)
            else:
                self.filepath = ''
                self.name = ''
                self.cwd = self._get_file_start_dir('')
            self.icon = ''
            self.lastactive = time()
            self.history = []
            self.wrap = None

        self.colmapfg = colmapfg
        self.colmapbg = colmapbg
        self.shell = shell
        self.font_size = font_size
        self.max_history = max_history

        self._ansicache = {}

        self.stop_t = False
        self.btext = b''
        self.btextchanged = False
        self.block = Lock()
        self.block.acquire()

        self.ch_out = None
        self.ch_pid = -1
        self.p = None

        self.memo = None
        self.dirty = False

    def _init_memo(self):
        self.memo_wgt_name = Terminal._get_memo_name()

        n = dlg_proc(self.h_dlg, DLG_CTL_ADD, 'editor')
        dlg_proc(self.h_dlg, DLG_CTL_PROP_SET, index=n, prop={
            'name': self.memo_wgt_name,
            'a_t': ('', '['),
            'a_l': ('', '['),
            'a_r': ('', ']'),
            'a_b': ('panels_parent', '['),
            'font_size': self.font_size,
            })
        self.memo = Editor(dlg_proc(self.h_dlg, DLG_CTL_HANDLE, index=n))

        self.memo.set_prop(PROP_RO, True)
        self.memo.set_prop(PROP_UNDO_LIMIT, 0) # Undo gives markers leaks
        self.memo.set_prop(PROP_CARET_VIRTUAL, False)
        self.memo.set_prop(PROP_GUTTER_ALL, False)
        self.memo.set_prop(PROP_UNPRINTED_SHOW, False)
        self.memo.set_prop(PROP_MARGIN_STRING, '')
        self.memo.set_prop(PROP_LAST_LINE_ON_TOP, False)
        self.memo.set_prop(PROP_HILITE_CUR_LINE, False)
        self.memo.set_prop(PROP_HILITE_CUR_COL, False)
        self.memo.set_prop(PROP_MODERN_SCROLLBAR, True)
        self.memo.set_prop(PROP_MINIMAP, False)
        self.memo.set_prop(PROP_MICROMAP, False)
        self.memo.set_prop(PROP_LINKS_REGEX, 'a^') # match nothing - disable clickable links
        self._update_memo_colors()

        wrap = self.wrap or TERM_WRAP
        self._apply_wrap(wrap)

    def _update_memo_colors(self):
        if self.memo:
            self.memo.set_prop(PROP_COLOR, (COLOR_ID_TextBg, self.colmapbg['default']))
            self.memo.set_prop(PROP_COLOR, (COLOR_ID_TextFont, self.colmapfg['default']))

    def _open_terminal(self, columns=1024, lines=24):
        # child gets pid=0, fd=invalid;
        # parent: pid=child's, fd=connected to child's terminal
        p_pid, master_fd = pty.fork()

        if p_pid == 0:  # Child.
            if self.cwd:
                os.chdir(self.cwd)

            argv = shlex.split(self.shell)
            env = dict(TERM="xterm-color",
                        COLUMNS=str(columns), LINES=str(lines))
            if 'HOME' in os.environ:
                env['HOME'] = os.environ['HOME']
            if IS_MAC:
                env['PATH'] = os.environ.get('PATH', '') + ':/usr/local/bin:/usr/local/sbin:/opt/local/bin:/opt/local/sbin'

            _loc = {k:v for k,v in os.environ.items()  if k.startswith('LC_')}
            env.update(_loc)

            os.execvpe(argv[0], argv, env)

        # File-like object for I/O with the child process aka command.
        self.ch_out = os.fdopen(master_fd, "w+b", 0)
        self.ch_pid = p_pid

    def _open_process(self):
        self.p = Popen(
            os.path.expandvars(self.shell), #TODO Windows: use shell arguments
            stdin = PIPE,
            stdout = PIPE,
            stderr = STDOUT,
            shell = IS_WIN,
            bufsize = 0,
            env = os.environ,
            cwd = self.cwd,
            )

    def _apply_wrap(self, wrap):
        if wrap == 'char':
            self.memo.set_prop(PROP_WRAP, WRAP_ON_MARGIN)
            self.memo.set_prop(PROP_MARGIN, 2000)
        elif wrap == 'word':
            self.memo.set_prop(PROP_WRAP, WRAP_ON_WINDOW)
            self.memo.set_prop(PROP_MARGIN, 2000)
        elif wrap == 'off':
            self.memo.set_prop(PROP_WRAP, WRAP_OFF)
            self.memo.set_prop(PROP_MARGIN, 2000)
        elif type(wrap) == int:
            self.memo.set_prop(PROP_WRAP, WRAP_ON_MARGIN)
            self.memo.set_prop(PROP_MARGIN, wrap)

    def show(self):
        if not self.memo:
            self._init_memo()

            if IS_WIN:
                self._open_process()
            else:
                self._open_terminal(lines=TERM_HEIGHT)

            log('* opened terminal: ' + str(self.name))

            self.CtlTh = ControlTh(self)
            self.CtlTh.start()
            log(' + started thread: ' + str(self.name))

        self.lastactive = time()

        h_ed = self.memo.get_prop(PROP_HANDLE_SELF)
        dlg_proc(self.h_dlg, DLG_CTL_PROP_SET, name=self.memo_wgt_name, prop={'vis':True})

    def hide(self):
        if self.memo:
            h_ed = self.memo.get_prop(PROP_HANDLE_SELF)
            dlg_proc(self.h_dlg, DLG_CTL_PROP_SET, name=self.memo_wgt_name, prop={'vis':False})

    def exit(self):
        if self.memo:
            self.memo = None
            dlg_proc(self.h_dlg, DLG_CTL_DELETE, name=self.memo_wgt_name)

        self.stop_t = True # stop thread
        self.stop()

        if self.block.locked(): # allow control thread to stop
            self.block.release()

    def stop(self):
        ch_pid = self.ch_pid
        ch_out = self.ch_out
        self.ch_pid = -1
        self.ch_out = None

        if ch_pid >= 0:
            # SIGTERM doesnt kill bash if bash has something running => hangs on waitpid()
            #   ... close properly is an unreliable pain
            #os.kill(self.ch_pid, signal.SIGTERM)
            os.kill(ch_pid, signal.SIGKILL)
            os.waitpid(ch_pid, 0) # otherwise get a zombie;  0 - normal operation
        if ch_out:
            ch_out.close()
        if self.p:
            if IS_WIN:
                startupinfo = STARTUPINFO()
                startupinfo.dwFlags |= STARTF_USESHOWWINDOW
                call(['taskkill', '/F', '/T', '/PID',  str(self.p.pid)], startupinfo=startupinfo)
            else:
                self.p.terminate()
                self.p.wait()


    def restart_shell(self):
        log('* Restarting shell: ' + str(self.name))
        self.stop()

        # restarting (should preserve previous output...)
        #self.block.acquire()

        if IS_WIN:
            self._open_process()
        else:
            self._open_terminal(lines=TERM_HEIGHT)

    def set_wrap(self, wrap):
        self.wrap = wrap
        wrap = self.wrap or TERM_WRAP
        self._apply_wrap(wrap)

    def add_to_history(self, s):
        if self.max_history > 0:
            if s in self.history:
                self.history.remove(s)
            self.history.append(s)

            if len(self.history) > self.max_history:
                del self.history[:-self.max_history]

    def get_display_path(self):
        return pretty_path(self.filepath or self.cwd)

    def get_state(self):
        state = {}
        state['filepath'] = pretty_path(self.filepath)
        state['name'] = self.name
        state['cwd'] = pretty_path(self.cwd)
        state['lastactive'] = self.lastactive

        state['icon'] = self.icon
        state['history'] = self.history[-self.max_history:]  if self.max_history > 0 else  []

        if self.wrap:
            state['wrap'] = self.wrap

        return state

    def get_memo_sroll_vert(self):
        """ returns (pos, max pos)
        """
        info = self.memo.get_prop(PROP_SCROLL_VERT_INFO)
        return info['smooth_pos'], info['smooth_pos_last']

    def _get_memo_name():
        ind = Terminal.memo_count
        Terminal.memo_count += 1
        return 'memo' + str(ind)

    def _get_file_start_dir(self, filepath):
        if SHELL_START_DIR == 'project':
            import cuda_project_man as p
            if p.global_project_info:
                mf = p.global_project_info.get('mainfile')
                if mf:
                    return os.path.dirname(mf)

        if SHELL_START_DIR == 'file' and filepath:
            return os.path.dirname(filepath)

        if SHELL_START_DIR == 'user' or not filepath:
            return HOMEDIR

        return os.path.dirname(filepath)


class TerminalBar:
    def __init__(self, h_dlg, plugin, shell_str, state, layout, max_history, font_size):
        self.Cmd = plugin
        self.h_dlg = h_dlg
        self.shell_str = shell_str
        self.state = state # list of dicts
        self.font_size = font_size
        self.max_history = max_history

        self.start_extras = 1 # non-terminal cells at start
        self.end_extras = 2 # at end

        self.h_iml, self.ic_inds = self._load_icons()
        self.ic_inds_util = {
            'no_icon':self.ic_inds.pop('White'),  # default|initial icon
            'ic_pluss':self.ic_inds.pop('ic_pluss'),
            'ic_cross':self.ic_inds.pop('ic_cross'),
        }
        self._update_colors()

        self.terminals = [] # list of Terminal()
        self.sidebar_names = []
        self.active_term = None # Terminal()
        self._init_terms(state)

        self.h_sb = self._open_init()

        self.refresh()
        # ignore show_term 0.5 sec after start (to not override active_term by initial panel)
        self._start_time = time()
        self._apply_layout(layout)

        self.on_theme_change() # to apply statusbar border color

    def _init_terms(self, state):
        if state:
            # which to activate:
            #   * last active for current Editor
            #   * no file in current Editor => just last active

            currentfilepath = ed.get_filename()
            if currentfilepath:
                currenttab_states = [ts for ts in self.state  if ts['filepath'] == currentfilepath]
                if currenttab_states:
                    max_lastactive = max((termstate['lastactive'] for termstate in currenttab_states))
                else:
                    max_lastactive = max((termstate['lastactive'] for termstate in state))
            else: # no-file tab
                max_lastactive = max((termstate['lastactive'] for termstate in state))

            for termstate in self.state:
                # create terminal
                term = Terminal(self.h_dlg, filepath=None, shell=self.shell_str, font_size=self.font_size,
                                    colmapfg=self.Cmd.colmapfg, colmapbg=self.Cmd.colmapbg, state=termstate,
                                    max_history=self.max_history)
                self.terminals.append(term)

                if termstate['lastactive'] == max_lastactive:
                    self.active_term = term

        else: # no state
            # create no-file terminal
            term = Terminal(self.h_dlg, filepath=None, shell=self.shell_str, font_size=self.font_size,
                        colmapfg=self.Cmd.colmapfg, colmapbg=self.Cmd.colmapbg, max_history=self.max_history)
            self.terminals.append(term)
            self.active_term = term

        TerminalBar._sort_terms(self.terminals)

    def _open_init(self):
        n = dlg_proc(self.h_dlg, DLG_CTL_ADD, 'statusbar')
        dlg_proc(self.h_dlg, DLG_CTL_PROP_SET, index=n, prop={
            'name': 'statusbar',
            'p': 'panels_parent',
            'h': TERMBAR_H,
            'align': self.Cmd._layout,
            'font_size': self.font_size,
            'color': self.Cmd.color_btn_back,
            'on_menu': cb_fs.format(cmd='on_statusbar_menu')
            })
        h_sb = dlg_proc(self.h_dlg, DLG_CTL_HANDLE, index=n)

        ### Icons ###
        statusbar_proc(h_sb, STATUSBAR_SET_IMAGELIST, value=self.h_iml)

        ### Plus, Spacer, Close ###
        # pluss
        cellind = statusbar_proc(h_sb, STATUSBAR_ADD_CELL, index=-1)
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_IMAGEINDEX, index=cellind, value=self.ic_inds_util['ic_pluss'])
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_AUTOSIZE, index=cellind, value=True)
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_COLOR_BACK, index=cellind, value=self.Cmd.color_tab_passive)
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_ALIGN, index=cellind, value='C')
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_HINT, index=cellind,
                                        value=_('Add terminal for current document (will be ^free for Untitled)'))
        callback = cbi_fs.format(cmd='on_statusbar_cell_click', info='new_term')
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_CALLBACK, index=cellind, value=callback)

        # spacer
        cellind = statusbar_proc(h_sb, STATUSBAR_ADD_CELL, index=-1)
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_AUTOSTRETCH, index=cellind, value=True)
        # cross
        cellind = statusbar_proc(h_sb, STATUSBAR_ADD_CELL, index=-1)
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_IMAGEINDEX, index=cellind, value=self.ic_inds_util['ic_cross'])
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_AUTOSIZE, index=cellind, value=True)
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_COLOR_BACK, index=cellind, value=self.Cmd.color_tab_passive)
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_ALIGN, index=cellind, value='C')
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_HINT, index=cellind, value=_('Close all terminals...'))
        callback = cb_fs.format(cmd='close_all_terms_dlg')
        statusbar_proc(h_sb, STATUSBAR_SET_CELL_CALLBACK, index=cellind, value=callback)

        return h_sb

    def _load_icons(self):
        h_iml = app_proc(PROC_SIDEPANEL_GET_IMAGELIST, '')

        res = {} # name -> image list index
        exts = ('.png', '.bmp')

        res['ic_pluss'] = imagelist_proc(h_iml, IMAGELIST_ADD, fn_icon_pluss)
        res['ic_cross'] = imagelist_proc(h_iml, IMAGELIST_ADD, fn_icon_cross)

        for folder in ICON_FOLDERS:
            if not os.path.exists(folder):
                continue

            for filename in os.listdir(folder):
                name, ext = os.path.splitext(filename)

                if ext.lower() in exts:
                    iconname = name.capitalize()
                    icon_path = os.path.join(folder, filename)
                    res[iconname] = imagelist_proc(h_iml, IMAGELIST_ADD, icon_path)
        return h_iml, res

    def _update_term_icons(self):
        # delete extra panels
        if len(self.terminals) < len(self.sidebar_names):
            todeln = len(self.sidebar_names) - len(self.terminals)
            for i in range(todeln):
                if len(self.sidebar_names) == 1: # to not remove last sidebar icon
                    break
                sidebar_name = self.sidebar_names.pop()
                app_proc(PROC_BOTTOMPANEL_REMOVE, sidebar_name)
            log('      new sidebar count: {0}: {1}'.format(len(self.sidebar_names), self.sidebar_names))

        taken_names = set()

        no_icon = self.ic_inds_util['no_icon']
        # update cell icons  and add sidebar icons
        for i,term in enumerate(self.terminals):
            cellind = self.start_extras + i

            icon = self.ic_inds.get(term.icon, no_icon)

            statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_IMAGEINDEX, index=cellind, value=icon)

            # sidebar
            displ_path = term.get_display_path()

            panelname = 'Terminal+' if i == 0 else 'Terminal+'+str(i)
            tooltip = 'Terminal: ' + displ_path
            ind = 2
            while tooltip in taken_names:
                tooltip = 'Terminal '+ str(ind) + ": " + displ_path
                ind += 1
            taken_names.add(tooltip)

            # add if need more sidebar tabs
            if i >= len(self.sidebar_names):
                if not self.Cmd.floating: # sidebar icons are useless when .floating=True
                    app_proc(PROC_BOTTOMPANEL_ADD_DIALOG, (panelname, self.h_dlg, fn_icon))
                self.sidebar_names.append(panelname)

            # sidebar hint
            app_proc(PROC_BOTTOMPANEL_SET_PROP, (panelname, icon, tooltip))

        if not self.terminals:
            app_proc(PROC_BOTTOMPANEL_SET_PROP,
                    (self.sidebar_names[0], self.ic_inds_util['no_icon'], 'Terminal+'))

    def _update_statusbar_cells_bg(self):
        if not hasattr(self, 'h_sb'):
            return

        editor_filepath = ed.get_filename()

        # for 'zebra'
        term_file_ind = {}
        for term in self.terminals:
            if term.filepath not in term_file_ind:
                term_file_ind[term.filepath] = len(term_file_ind)

        opened_files = {Editor(h).get_filename() for h in ed_handles()}
        opened_files.add('')

        for i,term in enumerate(self.terminals):
            cellind = i + self.start_extras

            linecol_u = 0x1fffffff # default line color
            if self.active_term == term: # active term
                linecol_u = self.color_cell_bright

            if term.filepath == editor_filepath: # current file
                linecol_d = self.color_cell_bright
            elif term.filepath not in opened_files: # file not opened in editor
                linecol_d = self.color_cell_err
            else: # norm
                is_even = (term_file_ind[term.filepath]%2) == 0
                linecol_d = 0x1fffffff  if is_even else  self.color_cell_dark

            statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_COLOR_LINE, index=cellind, value=linecol_u)
            statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_COLOR_LINE2, index=cellind, value=linecol_d)

            statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_COLOR_BACK, index=cellind, value=self.color_cell_bg)

    # calculate cell colors
    def _update_colors(self):
        v_zebra = max(0, min(0.5, ZEBRA_LIGHTNESS_DELTA*0.01)) # clamp to 0.0-0.5

        # 'Lightness' shift (from HSV color); values are relative to active tab color from current theme
        v_cell_bg = 0
        v_cell_dark = -v_zebra
        v_cell_bright = 0.15

        v_max = max(v_cell_bg, v_cell_dark, v_cell_bright)
        v_min = min(v_cell_bg, v_cell_dark, v_cell_bright)

        col_tab = MColor(self.Cmd.color_tab_active)
        if col_tab.v() + v_max > 1:
            col_tab.v(add=(1-v_max)-col_tab.v())
        elif col_tab.v() + v_min + 0.1 < 0:
            col_tab.v(add=(0.1+v_min)-col_tab.v())
        col_tab.v(add=v_cell_bg)

        col_even = MColor(src=col_tab)
        col_even.v(add=v_cell_dark)

        col_active = MColor(src=col_tab)
        col_active.v(add=v_cell_bright)

        # red-ish, try to preserve Saturation and Lightness of tab-color
        col_nofile = MColor(src=col_tab)
        cnf_h, cnf_s, cnf_v = col_nofile.hsv()
        cnf_h = 0 # red
        _len = (1-cnf_s) + (1-cnf_v)
        if _len > 0.85: # 0.15 is min saturation and lightness
            mult = 0.85/_len
            cnf_s = 1-((1-cnf_s)*mult)
            cnf_v = 1-((1-cnf_v)*mult)
        col_nofile.set_hsv((cnf_h, cnf_s, cnf_v))

        self.color_cell_bg = col_tab.hexcol() # cell bg
        self.color_cell_bright = col_active.hexcol() # current file of selected terminal
        self.color_cell_dark = col_even.hexcol() # zebra
        self.color_cell_err = col_nofile.hexcol() # terminal for not-open file

    def _show_terminal(self, ind, focus_input=True):
        self.Cmd.memo = None
        changing_term = False
        if self.active_term  and self.active_term != self.terminals[ind]:
            self.active_term.hide()
            changing_term = True

        self.active_term = self.terminals[ind]
        if not self.active_term.memo  or self.Cmd.memo != self.active_term.memo:
            self.active_term.show()
            self.Cmd.memo = self.active_term.memo

            self._update_term_icons()
            self._update_statusbar_cells_bg()
            self.Cmd.upd_history_combo()

        if focus_input:
            self.Cmd.queue_focus_input()

    def _apply_layout(self, layout):
        count = statusbar_proc(self.h_sb, STATUSBAR_GET_COUNT)
        spacer_ind = count-2

        if layout == ALIGN_TOP: # vertical
            statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_AUTOSTRETCH, index=spacer_ind, value=True)
        else: # horizontal
            statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_AUTOSTRETCH, index=spacer_ind, value=False)
            statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_SIZE, index=spacer_ind, value=0)

    def _sort_terms(l):
        # sort: no-file terms | terms with editor tabs | terms without editor tabs
        fileinds = {}
        for i,h in enumerate(ed_handles()):
            filepath = Editor(h).get_filename()
            if filepath:
                fileinds[filepath] = i

        l.sort(key=lambda term: fileinds.get(term.filepath, (-1  if not term.filepath else 1000)))

    def refresh(self):
        needtermsn = len(self.terminals)
        termsn = statusbar_proc(self.h_sb, STATUSBAR_GET_COUNT) - self.start_extras - self.end_extras

        # proper number of cells
        if needtermsn > termsn: # need more terminals
            for i in range(needtermsn - termsn):
                add_ind = self.start_extras + termsn + i
                termind = add_ind - self.start_extras
                cellind = statusbar_proc(self.h_sb, STATUSBAR_ADD_CELL, index=add_ind, tag=termind)

                statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_AUTOSIZE, index=cellind, value=True)
                callback = cbi_fs.format(cmd='on_statusbar_cell_click', info=termind)
                statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_CALLBACK, index=cellind, value=callback)
                statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_ALIGN, index=cellind, value='C')

        elif needtermsn < termsn: # need less terms
            rem_ind = self.start_extras + needtermsn
            for i in range(termsn - needtermsn):
                statusbar_proc(self.h_sb, STATUSBAR_DELETE_CELL, index=rem_ind)

        for i,term in enumerate(self.terminals):
            cellind = i + self.start_extras

            if len(term.name) <= MAX_TERM_NAME_LEN:
                text = term.name
            else:
                text = term.name[:MAX_TERM_NAME_LEN-1] + '..'

            if not term.filepath:
                text = '^'+text

            statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_TEXT, index=cellind, value=text)

            hint = 'Terminal+: ' + term.get_display_path()
            statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_HINT, index=cellind, value=hint)

        self._update_term_icons()
        self._update_statusbar_cells_bg()
        self.Cmd._queue_layout_controls()
        self.Cmd.upd_history_combo()


    # [(140617069637616, 2, 2, 'ind2')], [{}]
    def on_statusbar_cell_click(self, id_dlg, id_ctl, data='', info=''):
        log(' cell click:{0}; {1}'.format(data, info))

        if info == 'new_term':
            filepath = ed.get_filename()
            self.new_term(filepath=filepath)
        else:
            clicked_ind = data
            self._show_terminal(clicked_ind)

    # data:{'btn': 1, 'state': '', 'x': 19, 'y': 6}
    def on_statusbar_menu(self, id_dlg, id_ctl, data='', info=''):
        p = dlg_proc(self.h_dlg, DLG_CTL_PROP_GET, name='statusbar')
        termind = p["tab_hovered"] - self.start_extras
        if termind < 0  or termind >= len(self.terminals):
            return

        term = self.terminals[termind]

        # rename
        h_menu = menu_proc(0, MENU_CREATE)
        callback = cbi_fs.format(cmd='on_statusbar_cell_rename', info=termind)
        menu_proc(h_menu, MENU_ADD, command=callback, caption=_('Rename...'))

        # icon change
        ic_id = menu_proc(h_menu, MENU_ADD, caption=_('Change icon'))
        for icname in list(sorted(self.ic_inds)):
            callback = cbi_fs.format(cmd='on_set_term_icon', info=str(termind) + chr(1) + icname)
            menu_proc(ic_id, MENU_ADD, command=callback, caption=icname)

        # Terminal Wrap
        wrap_id = menu_proc(h_menu, MENU_ADD, caption=_('Terminal wrap'))

        callback = cbi_fs.format(cmd='on_set_term_wrap', info=str(termind) + chr(1) + 'off')
        wrap_none_id = menu_proc(wrap_id, MENU_ADD, command=callback, caption=_('No wrap'))

        callback = cbi_fs.format(cmd='on_set_term_wrap', info=str(termind) + chr(1) + 'char')
        wrap_char_id = menu_proc(wrap_id, MENU_ADD, command=callback, caption=_('By character'))

        callback = cbi_fs.format(cmd='on_set_term_wrap', info=str(termind) + chr(1) + 'word')
        wrap_word_id = menu_proc(wrap_id, MENU_ADD, command=callback, caption=_('By word'))

        callback = cbi_fs.format(cmd='on_set_term_wrap', info=str(termind) + chr(1) + 'custom')
        wrap_custom_caption = (_('Custom column: ')+str(term.wrap)+'...'  if type(term.wrap) == int
                                                                                else  _('Custom column...'))
        wrap_custom_id = menu_proc(wrap_id, MENU_ADD, command=callback, caption=wrap_custom_caption)

        menu_proc(wrap_id, MENU_ADD, caption='-') # separator

        callback = cbi_fs.format(cmd='on_set_term_wrap', info=str(termind) + chr(1))
        menu_proc(wrap_id, MENU_ADD, command=callback, caption=_('Reset'))

        if term.wrap:
            if term.wrap == 'char':
                checked_id = wrap_char_id
            elif term.wrap == 'word':
                checked_id = wrap_word_id
            elif term.wrap == 'off':
                checked_id = wrap_none_id
            else:
                checked_id = wrap_custom_id
            menu_proc(checked_id, MENU_SET_CHECKED, command=True)


        # close
        menu_proc(h_menu, MENU_ADD, caption='-') # separator
        callback = cbi_fs.format(cmd='on_statusbar_cell_close', info=termind)
        menu_proc(h_menu, MENU_ADD, command=callback, caption=_('Close'))

        menu_proc(h_menu, MENU_SHOW)

    def show_terminal(self, ind=None, name=None):
        if not self.terminals:
            log('* termbar: show_terminal: NO TERMINALS')
            return
        if time() - self._start_time < 0.5:
            log('* sklipping show_term: too soon') #TODO check if still needed

            if self.terminals and self.active_term:
                self._show_terminal(self.terminals.index(self.active_term))
            return

        log('* Show Term:{0}, {1}'.format(ind, name))

        if ind is None and name is not None:
            if name.startswith('Terminal+'): # in floating mode my panel might not be active
                ind = 0  if name == 'Terminal+' else  int(name.split('Terminal+')[1])
            elif self.active_term and self.active_term in self.terminals:
                ind = self.terminals.index(self.active_term)

        self._show_terminal(ind)

    def remove_term(self, term, show_next=False):
        term.exit()

        if term in self.terminals:
            if show_next:
                curterms = [t for t in self.terminals  if t.filepath == term.filepath]
                if len(curterms) > 1:
                    choiceterms = curterms
                else:
                    choiceterms = self.terminals

                ind = choiceterms.index(term)
                termsn = len(choiceterms)

                if termsn > 1:
                    nextind = ind - 1  if (ind == termsn - 1) else  ind + 1
                    ind = self.terminals.index(choiceterms[nextind])
                    self._show_terminal(ind=self.terminals.index(choiceterms[nextind]))

            self.terminals.remove(term)

            self.Cmd._queue_layout_controls()

        if self.active_term == term:
            self.active_term = None

    def close_all(self):
        for term in [*self.terminals]:
            self.remove_term(term)
        self.refresh()

        if not self.Cmd.floating:
            activate_bottompanel(self.sidebar_names[0])

    def new_term(self, filepath):
        log('* new term for: ' + str(filepath))

        term = Terminal(self.h_dlg, filepath=filepath, shell=self.shell_str, font_size=self.font_size,
                            colmapfg=self.Cmd.colmapfg, colmapbg=self.Cmd.colmapbg, max_history=self.max_history)
        self.terminals.append(term)
        TerminalBar._sort_terms(self.terminals)
        self.refresh()
        self._show_terminal(self.terminals.index(term))

    def set_term_icon(self, ind, icname):
        self.terminals[ind].icon = icname
        self._update_term_icons()

    def set_term_wrap(self, ind, wrap):
        self.terminals[ind].set_wrap(wrap)

    def timer_update(self):
        if not self.active_term:
            return False # do not update memo

        self.active_term.btextchanged = False
        if self.active_term.block.locked():
            self.active_term.block.release()
        sleep(0.03)
        self.active_term.block.acquire()
        return self.active_term.btextchanged

    def get_active_term(self):
        return self.active_term

    def get_active_sidebar(self):
        if not self.Cmd.floating  and self.active_term and  self.active_term in self.terminals:
            return self.sidebar_names[self.terminals.index(self.active_term)]
        return self.sidebar_names[0]

    # list of term-dicts
    def get_state(self):
        l = []
        for term in self.terminals:
            l.append(term.get_state())
        return l

    def get_children_w(self):
        count = statusbar_proc(self.h_sb, STATUSBAR_GET_COUNT)
        full_w = 0
        for i in range(count):
            full_w += statusbar_proc(self.h_sb, STATUSBAR_GET_CELL_SIZE, i)
        return full_w

    def on_tab_reorder(self):
        if self.terminals:
            TerminalBar._sort_terms(self.terminals)
            self.refresh()
            self._show_terminal(self.terminals.index(self.active_term), focus_input=False)

    def on_tab_change(self):
        self._update_statusbar_cells_bg()
        self._update_term_icons()

    def on_theme_change(self, update_terminals=False):
        self._update_colors()

        self._update_statusbar_cells_bg()

        # misc
        dlg_proc(self.h_dlg, DLG_CTL_PROP_SET, name='statusbar', prop={
            'color': self.Cmd.color_btn_back,
            })
        count = statusbar_proc(self.h_sb, STATUSBAR_GET_COUNT)
        statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_COLOR_BACK, index=0, value=self.Cmd.color_tab_passive)
        statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_COLOR_BACK, index=count-1, value=self.Cmd.color_tab_passive)

        #statusbar_proc(self.h_sb, STATUSBAR_SET_COLOR_BORDER_L, value=self.Cmd.color_tab_border_active)
        statusbar_proc(self.h_sb, STATUSBAR_SET_COLOR_BORDER_R, value=self.Cmd.color_tab_border_active)

        if update_terminals:
            for term in self.terminals:
                if term is not self.active_term:
                    term.dirty = True

    def on_exit(self):
        for term in self.terminals:
            term.exit()

    def run_cmd(self, cmd, **vargs):
        log('* termbar.run_cmd:{0} ({1})'.format(cmd, vargs))

        if cmd == CMD_CLOSE_LAST_CUR_FILE:
            curfilepath = ed.get_filename()
            if not curfilepath: return

            for term in reversed(self.terminals):
                if term.filepath == curfilepath:
                    self.remove_term(term, show_next=True)
                    self.refresh()
                    self._update_term_icons()
                    self._update_statusbar_cells_bg()
                    if not self.Cmd.floating:
                        activate_bottompanel(self.get_active_sidebar())
                    break

        elif cmd == CMD_CLOSE:
            term = None # in case of no terms
            if 'ind' in vargs: # context menu
                term = self.terminals[vargs['ind']]
            elif self.active_term:
                term = self.active_term

            if term:
                self.remove_term(term, show_next=True)
                self.refresh()
                self._update_term_icons()
                self._update_statusbar_cells_bg()
                if not self.Cmd.floating:
                    activate_bottompanel(self.get_active_sidebar())

        elif cmd == CMD_CUR_FILE_TERM_SWITCH:
            is_ed_focused = vargs['is_ed_focused']

            is_term_focused = self.Cmd.is_focused()

            if is_ed_focused:  # focus editor-file's last used terminal if any
                if self.terminals:
                    filepath = ed.get_filename()
                    if filepath:
                        term_times = {term.lastactive:ind for ind,term in enumerate(self.terminals)
                                            if term.filepath == filepath}
                        if term_times:
                            last_file_term_ind = term_times[max(term_times)]
                            self.show_terminal(ind=last_file_term_ind)
                            #panel_name = (self.sidebar_names[last_file_term_ind] if not self.Cmd.floating
                                                                            #else self.get_active_sidebar())
                            #activate_bottompanel(panel_name)
                            self.Cmd.ensure_shown()

                            self.Cmd.queue_focus_input(force=True)
                        else:
                            print(_('Document has no terminals: ')+filepath)

            elif is_term_focused: # focus terminal's editor if any
                if self.active_term and self.active_term.filepath:
                    for h in ed_handles():
                        e = Editor(h)
                        if e.get_filename() == self.active_term.filepath:
                            e.focus()
                            break
                    else:
                        file_open(self.active_term.filepath)
                        ed.focus()
            else: # focused not editor or term: focus editor
                ed.focus()

        elif cmd == CMD_NEXT:
            if self.active_term and self.terminals and self.active_term in self.terminals:
                nexttermind = (self.terminals.index(self.active_term)+1) % len(self.terminals)
                self._show_terminal(nexttermind)

        elif cmd == CMD_PREVIOUS:
            if self.active_term and self.terminals and self.active_term in self.terminals:
                prevtermind = (self.terminals.index(self.active_term)+(len(self.terminals)-1)) % len(self.terminals)
                self._show_terminal(prevtermind)

        elif cmd == CMD_EXEC_SEL:
            if self.active_term and len(ed.get_carets()) == 1:
                txt = ed.get_text_sel()
                if txt:
                    if '\n' not in txt:
                        self.Cmd.run_cmd(txt)
                else:
                    caret = ed.get_carets()[0]
                    caret_x,caret_y = caret[0:2]
                    txt = ed.get_text_line(caret_y).rstrip('\n')
                    if txt:
                        self.Cmd.run_cmd(txt)
                    if caret_y+1 < ed.get_line_count():
                        ed.set_caret(caret_x, caret_y+1)

        elif cmd == CMD_RENAME:
            termind = vargs.get('ind', -1) # -1 - current

            if termind >= 0: # context menu
                term = self.terminals[termind]
            elif self.active_term: # comamnd (menu, ...)
                term = self.active_term

            if term:
                newname = dlg_input(_('Rename terminal:'), term.name)
                if newname is not None:
                    term.name = newname
                    self.refresh()
                    self._update_term_icons()
                    self._update_statusbar_cells_bg()

    def _dbg_set_cells_col(self, col):
        count = statusbar_proc(self.h_sb, STATUSBAR_GET_COUNT)
        for i in range(count):
            statusbar_proc(self.h_sb, STATUSBAR_SET_CELL_COLOR_BACK, index=i, value=col)

    """ # scroll hidden terms, later
    _hidden = 0
    def _dbg_toggle_term_hide(self, hide):
        count = statusbar_proc(self.h_sb, STATUSBAR_GET_COUNT)
        count -= self.start_extras + self.end_extras
        if hide:
            tohide = self._hidden"""

class Command:
    broke_col = 0xff007f # pink

    colmapfg = { # xterm... https://en.wikipedia.org/wiki/ANSI_escape_code#Colors
        'black': 0x36342e, # black     0 0 0
        'red': 0x0000cc, # red       205,0,0
        'green': 0x69a4e, # green     0 205 0
        'brown': 0x00a0c4, # yellow    205 205 0
        'blue': 0xa46534, # blue      0 0 238
        'magenta': 0x7b5075, # magenta   205 0 205
        'cyan': 0x9a9806, # cyan      0 205 205
        'white': 0xcfd7d3, # white     229 229 229

        'brightblack': 0x535755, #### bblack 127 127 127 (bright)
        'brightred': 0x2929ef, # bred      255 0 0
        'brightgreen': 0x34e28a, # bgreen    0 255 0
        'brightbrown': 0x4fe9fc, # byellow   255 255 0
        'brightblue': 0xcf9f72, # bblue     92 92 255
        'brightmagenta': 0xa87fad, # bmagenta  255 0 255
        'brightcyan': 0xe2e234, # bcyan     0 255 255
        'brightwhite': 0xeceeee, # bwhite    255 255 255

        'default': 0xcfd7d3, # white     229 229 229
    }
    colmapbg = {
        'black': 0x36342e,
        'red':  0x0000cc,
        'green':  0x69a4e,
        'brown':  0x00a0c4,
        'blue':  0xa46534,
        'magenta':  0x7b5075,
        'cyan':  0x9a9806,
        'white':  0xcfd7d3,

        'brightblack': 0x535755,
        'brightred': 0x2929ef,
        'brightgreen': 0x34e28a,
        'brightbrown': 0x4fe9fc,
        'brightblue': 0xcf9f72,
        'brightmagenta': 0xa87fad,
        'brightcyan': 0xe2e234,
        'brightwhite': 0xeceeee,

        'default': 0x240a30, # purple from ubuntu theme
    }


    def __init__(self):
        self.title = 'Terminal+'
        self.title_float = _('CudaText Terminal')
        self.hint_float = _('Terminal opened in floating window')
        self.h_dlg = None

        self.terminal_w = 2048 # - unlimited?
        self.termbar = None

        self._get_theme_colors()
        self._load_config()

        self.load_history()
        self.h_menu = menu_proc(0, MENU_CREATE)

        self._is_shown = False
        self._last_cmd = None # (cmd, id(terminal))

        max_menu_size = self.max_history_loc  if self.max_history_loc > 0 else  HISTORY_GLOBAL_TAIL_LEN
        self.menu_calls = [(lambda ind=i:self.run_cmd_n(ind)) for i in range(max_menu_size)]

    def _get_theme_colors(self):
        colors = app_proc(PROC_THEME_UI_DICT_GET, '')
        self.color_btn_back = colors['ButtonBgPassive']['color']
        self.color_btn_font = colors['ButtonFont']['color']
        self.color_tab_active = colors['TabActive']['color']
        self.color_tab_passive = colors['TabPassive']['color']
        self.color_tab_border_active = colors['TabBorderActive']['color']
        self.color_tab_border_passive = colors['TabBorderPassive']['color']
        self.color_ed_bg = colors['EdTextBg']['color']
        self.color_ed_fg = colors['EdTextFont']['color']

        return colors

    def _load_term_theme(self):
        """ return True if using editor colors
        """
        colnames = ['black', 'red', 'green', 'brown', 'blue', 'magenta', 'cyan', 'white', 'brightblack',
            'brightred', 'brightgreen', 'brightbrown', 'brightblue', 'brightmagenta', 'brightcyan',
            'brightwhite', 'default', ]

        fg_spl = self.theme_str_fg.split(',')
        bg_spl = self.theme_str_bg.split(',')
        if len(colnames) == len(fg_spl) and len(colnames) == len(bg_spl):
            using_ed_col = False
            for i,(name,s_fg,s_bg) in enumerate(zip(colnames, fg_spl, bg_spl)):
                try:
                    fgcol = html_color_to_int(s_fg)
                except Exception:
                    if len(colnames)-1 != i:
                        raise
                    fgcol = self.color_ed_fg
                    using_ed_col = True

                try:
                    bgcol = html_color_to_int(s_bg)
                except Exception:
                    if len(colnames)-1 != i:
                        raise
                    bgcol = self.color_ed_bg
                    using_ed_col = True

                self.colmapfg[name] = fgcol
                self.colmapbg[name] = bgcol

            return using_ed_col

    def _open_init(self):
        self.h_dlg, self.h_panels_parent = self._init_form()

        if self.floating:
            self._load_pos()
            dlg_proc(self.h_dlg, DLG_PROP_SET, prop={
                'border': DBORDER_SIZE,
                'cap': self.title_float,
                'x': self.wnd_x,
                'y': self.wnd_y,
                'w': self.wnd_w,
                'h': self.wnd_h,
                'topmost': self.floating_topmost,
            })
            dlg_proc(self.h_dlg, DLG_SHOW_NONMODAL)
            h_embed = dlg_proc(0, DLG_CREATE)
            n = dlg_proc(h_embed, DLG_CTL_ADD, prop='panel')
            dlg_proc(h_embed, DLG_CTL_PROP_SET, index=n, prop={
                'name': 'panel_placeholder',
                'cap': self.hint_float,
                'color': self.color_btn_back,
                'font_color': self.color_btn_font,
                'align': ALIGN_CLIENT,
            })
            self.h_embed = h_embed
        else:
            h_embed = self.h_dlg
            self.h_embed = None

        app_proc(PROC_BOTTOMPANEL_ADD_DIALOG, (self.title, h_embed, fn_icon))
        if self.floating: # fixes empty bottom panel on start (when floating)
            activate_bottompanel(self.termbar.get_active_sidebar())

        timer_proc(TIMER_START, self.timer_update, 200, tag='')

    def _init_form(self):
        cur_font_size = self.font_size

        h = dlg_proc(0, DLG_CREATE)
        dlg_proc(h, DLG_PROP_SET, prop={
            'border': False,
            'keypreview': True,
            'on_key_down': self.form_key_down,
            'on_show': self.form_show,
            'on_hide': self.form_hide,
            'color': self.color_btn_back,
            })

        # parent panels
        n = dlg_proc(h, DLG_CTL_ADD, 'panel')
        dlg_proc(h, DLG_CTL_PROP_SET, index=n, prop={
            'name': 'panels_parent',
            'a_l': ('', '['),
            'a_t': None,
            'a_r': ('', ']'),
            'a_b': ('', ']'),
            'h': INPUT_H*2,
            'cap': '',
            })
        h_panels_parent = dlg_proc(h, DLG_CTL_HANDLE, index=n)

        n = dlg_proc(h, DLG_CTL_ADD, 'panel')
        dlg_proc(h, DLG_CTL_PROP_SET, index=n, prop={
            'name': 'input_parent',
            'p': 'panels_parent',
            'align': ALIGN_CLIENT,
            'h': INPUT_H,
            'h_max': INPUT_H,
            'cap': '',
            })
        h_input_parent = dlg_proc(h, DLG_CTL_HANDLE, index=n)

        # widgets
        n = dlg_proc(h, DLG_CTL_ADD, 'button_ex')
        dlg_proc(h, DLG_CTL_PROP_SET, index=n, prop={
            'name': 'break',
            'p': 'input_parent',
            'a_l': None,
            'a_t': None,
            'a_r': ('', ']'),
            'a_b': ('', ']'),
            'w': 90,
            'h': INPUT_H,
            'cap': _('Break'),
            'hint': _('Hotkeys: Break or Ctrl+C'),
            'on_change': self.button_break_click,
            })

        n = dlg_proc(h, DLG_CTL_ADD, 'editor_combo')
        dlg_proc(h, DLG_CTL_PROP_SET, index=n, prop={
            'name': 'input',
            'p': 'input_parent',
            'h': INPUT_H,
            'a_l': ('', '['),
            'a_r': ('break', '['),
            'a_t': ('break', '-'),
            'font_size': cur_font_size,
            'texthint': _('Enter command here'),
            })
        self.input = Editor(dlg_proc(h, DLG_CTL_HANDLE, index=n))

        self.input.set_prop(PROP_ONE_LINE, True)
        self.input.set_prop(PROP_GUTTER_ALL, True)
        self.input.set_prop(PROP_GUTTER_NUM, False)
        self.input.set_prop(PROP_GUTTER_FOLD, False)
        self.input.set_prop(PROP_GUTTER_BM, False)
        self.input.set_prop(PROP_GUTTER_STATES, False)
        self.input.set_prop(PROP_UNPRINTED_SHOW, False)
        self.input.set_prop(PROP_MARGIN, 2000)
        self.input.set_prop(PROP_MARGIN_STRING, '')
        self.input.set_prop(PROP_HILITE_CUR_LINE, False)
        self.input.set_prop(PROP_HILITE_CUR_COL, False)
        self.input.set_prop(PROP_FONT, ed.get_prop(PROP_FONT, ''))


        termsstate = self._load_state()
        self.termbar = TerminalBar(h, plugin=self, shell_str=self.shell_str, state=termsstate,
                    layout=self._layout, max_history=self.max_history_loc, font_size=self.font_size)

        self._apply_layout_orientation(h_dlg=h, layout=self._layout)

        self.upd_history_combo()

        dlg_proc(h, DLG_SCALE)
        return h, h_panels_parent

    def _exec(self, s):
        term = self.termbar.get_active_term()

        if IS_WIN:
            if term.p and s:
                term.p.stdin.write((s+'\n').encode(ENC))
                term.p.stdin.flush()
        else:
            if term.ch_out and s:
                term.ch_out.write((s+'\n').encode(ENC))

    def _load_state(self):
        if os.path.exists(fn_state):
            with open(fn_state, 'r', encoding='utf-8') as f:
                return json.load(f)
        return []

    def _save_state(self):
        state = self.termbar.get_state()

        with open(fn_state, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)

    def _load_config(self):
        global ENC
        global MAX_BUFFER
        global ZEBRA_LIGHTNESS_DELTA
        global TERM_WRAP
        global SHELL_START_DIR
        global TERM_HEIGHT
        global LOCK_H_SCROLL

        if IS_WIN:
            ENC = ini_read(fn_config, 'op', 'encoding_windows', ENC)

        try:
            MAX_BUFFER = int(ini_read(fn_config, 'op', 'max_buffer_size', str(MAX_BUFFER)))
        except:
            pass

        SHELL_START_DIR = ini_read(fn_config, 'op', 'start_dir', SHELL_START_DIR)

        self.shell_unix = ini_read(fn_config, 'op', 'shell_unix', SHELL_UNIX)
        self.shell_mac = ini_read(fn_config, 'op', 'shell_macos', SHELL_MAC)
        self.shell_win = ini_read(fn_config, 'op', 'shell_windows', SHELL_WIN)
        self.floating = str_to_bool(ini_read(fn_config, 'op', 'floating_window', '0'))
        self.floating_topmost = str_to_bool(ini_read(fn_config, 'op', 'floating_window_topmost', '0'))
        self.layout_horizontal = str_to_bool(ini_read(fn_config, 'op', 'layout_horizontal', '0'))

        # theme
        self.theme_str_fg = ini_read(fn_config, 'op', 'shell_theme_fg', SHELL_THEME_FG)
        self.theme_str_bg = ini_read(fn_config, 'op', 'shell_theme_bg', SHELL_THEME_BG)
        self._load_term_theme()

        self._layout = ALIGN_RIGHT  if self.layout_horizontal else  ALIGN_TOP

        if IS_WIN:
            self.shell_str = self.shell_win
        else:
            self.shell_str = self.shell_mac  if IS_MAC else  self.shell_unix

        try:
            self.font_size = int(ini_read(fn_config, 'op', 'font_size', '9'))
        except:
            pass

        try:
            self.max_history_glob = int(ini_read(fn_config, 'op', 'global_history', '50'))
        except:
            pass

        try:
            self.max_history_loc = int(ini_read(fn_config, 'op', 'local_history', '10'))
        except:
            pass

        try:
            ZEBRA_LIGHTNESS_DELTA = int(ini_read(fn_config, 'op', 'terminal_bg_zebra', '8'))
        except:
            pass

        try:
            TERM_WRAP = int(ini_read(fn_config, 'op', 'wrap', TERM_WRAP))
        except:
            TERM_WRAP = ini_read(fn_config, 'op', 'wrap', TERM_WRAP)

        try:
            TERM_HEIGHT = int(ini_read(fn_config, 'op', 'terminal_height', str(TERM_HEIGHT)))
        except:
            pass

        _lock_h_scroll_s = ini_read(fn_config, 'op', 'lock_horizontal_scroll', bool_to_str(LOCK_H_SCROLL))
        LOCK_H_SCROLL = str_to_bool(_lock_h_scroll_s)

    def _load_pos(self):
        if not self.floating:
            return
        self.wnd_x = int(ini_read(fn_config, 'pos', 'x', '20'))
        self.wnd_y = int(ini_read(fn_config, 'pos', 'y', '20'))
        self.wnd_w = int(ini_read(fn_config, 'pos', 'w', '700'))
        self.wnd_h = int(ini_read(fn_config, 'pos', 'h', '400'))


    def _save_pos(self):
        if not self.floating:
            return

        p = dlg_proc(self.h_dlg, DLG_PROP_GET)
        x = p['x']
        y = p['y']
        w = p['w']
        h = p['h']

        ini_write(fn_config, 'pos', 'x', str(x))
        ini_write(fn_config, 'pos', 'y', str(y))
        ini_write(fn_config, 'pos', 'w', str(w))
        ini_write(fn_config, 'pos', 'h', str(h))

    def _queue_layout_controls(self):
        if self._layout == ALIGN_RIGHT:
            timer_proc(TIMER_START_ONE, callback=self._update_termbar_w, interval=150)

    # update horizontal layout when termbar content changed
    def _update_termbar_w(self, tag):
        parent_p = dlg_proc(self.h_dlg, DLG_CTL_PROP_GET, name='panels_parent')
        total_w = parent_p['w']
        max_termbar_w = int(total_w * 0.6)
        new_termbar_w = min(max_termbar_w, self.termbar.get_children_w())
        dlg_proc(self.h_dlg, DLG_CTL_PROP_SET, name='statusbar', prop={
                        'w': new_termbar_w})

    # applies layout properties, starts timer to set statusbar width to w of its children
    def _apply_layout_orientation(self, h_dlg, layout):
        if layout == ALIGN_TOP: # vertical
            parent_h = INPUT_H + TERMBAR_H + 4
        else: # horizontal
            parent_h = max(INPUT_H, TERMBAR_H)

            self._queue_layout_controls()

        dlg_proc(h_dlg, DLG_CTL_PROP_SET, name='statusbar', prop={
                    'align':self._layout})
        dlg_proc(h_dlg, DLG_CTL_PROP_SET, name='panels_parent', prop={
                    'h':parent_h})

    def config(self):
        ini_write(fn_config, 'op', 'shell_windows', self.shell_win)
        ini_write(fn_config, 'op', 'shell_unix', self.shell_unix)
        ini_write(fn_config, 'op', 'shell_macos', self.shell_mac)
        ini_write(fn_config, 'op', 'start_dir', SHELL_START_DIR)
        ini_write(fn_config, 'op', 'floating_window', bool_to_str(self.floating))
        ini_write(fn_config, 'op', 'floating_window_topmost', bool_to_str(self.floating_topmost))
        ini_write(fn_config, 'op', 'layout_horizontal', bool_to_str(self.layout_horizontal))
        ini_write(fn_config, 'op', 'font_size', str(self.font_size))
        ini_write(fn_config, 'op', 'max_buffer_size', str('{0:_}'.format(MAX_BUFFER))) # 100000 => 100_000
        ini_write(fn_config, 'op', 'global_history', str(self.max_history_glob))
        ini_write(fn_config, 'op', 'local_history', str(self.max_history_loc))
        ini_write(fn_config, 'op', 'shell_theme_fg', self.theme_str_fg)
        ini_write(fn_config, 'op', 'shell_theme_bg', self.theme_str_bg)
        ini_write(fn_config, 'op', 'terminal_bg_zebra', str(ZEBRA_LIGHTNESS_DELTA))
        ini_write(fn_config, 'op', 'wrap', str(TERM_WRAP))
        ini_write(fn_config, 'op', 'terminal_height', str(TERM_HEIGHT))
        ini_write(fn_config, 'op', 'lock_horizontal_scroll', bool_to_str(LOCK_H_SCROLL))

        if IS_WIN:
            ini_write(fn_config, 'op', 'encoding_windows', ENC)

        file_open(fn_config)

    def open(self):
        # dont init form twice!
        if not self.h_dlg:
            self._open_init()

        dlg_proc(self.h_dlg, DLG_CTL_FOCUS, name='input')

        if self.floating:
            # form can be hidden before, show
            dlg_proc(self.h_dlg, DLG_SHOW_NONMODAL)

            self.queue_focus_input()
        else:
            activate_bottompanel(self.termbar.get_active_sidebar())

    def ensure_shown(self):
        if not self._is_shown:
            self.open()
        if not self.floating:
            if not app_proc(PROC_SHOW_BOTTOMPANEL_GET, ''):
                app_proc(PROC_SHOW_BOTTOMPANEL_SET, True)
                self.queue_focus_input(force=True)

    def timer_update(self, tag='', info=''):
        changed = self.termbar.timer_update()

        # log("Entering in timer_update")
        if changed:
            self.update_output()

    # called on timer, if .btext changed
    def update_output(self):
        full_text, range_lists = self.parse_ansi_lines()

        h_pos = self.memo.get_prop(PROP_SCROLL_HORZ)  if LOCK_H_SCROLL else  None

        self.memo.set_prop(PROP_RO, False)
        self.memo.set_text_all(full_text)
        self.apply_colors(range_lists)
        self.memo.set_prop(PROP_RO, True)

        self.memo.cmd(cmds.cCommand_GotoTextEnd)
        self.memo.set_prop(PROP_LINE_TOP, self.memo.get_line_count()-3)
        if h_pos is not None:
            h_pos = self.memo.set_prop(PROP_SCROLL_HORZ, h_pos)

    def apply_colors(self, range_lists):
        # range_lists - map: (fg,bg,isbold) -> (xs,ys,lens)
        for (fg,bg,isbold),(xs,ys,lens) in range_lists.items():
            fgcol = self.colmapfg.get(fg, self.broke_col)
            bgcol = self.colmapbg.get(bg, self.broke_col)
            font_bold = 1 if isbold else 0

            self.memo.attr(MARKERS_ADD_MANY, x=xs, y=ys, len=lens,
                        color_font=fgcol, color_bg=bgcol, font_bold=font_bold)


    # cache - saves parsed lines, to not parse whole .btext after every shell command
    # term._ansicache = {} # byte line -> [(plain_str, color_ranges), ...]  # splitted by terminal width
    def parse_ansi_lines(self):
        """ parses terminal output bytes line-by-line, caching parsing-results per line
        """
        term = self.termbar.get_active_term()
#        # uncomment this to disable Escape Sequences on Windows
#        if IS_WIN:
#            return term.btext.decode(ENC, errors='replace'), {}

        blines = term.btext.split(b'\n')

        res = []
        cache_used = {}
        cache_new = {}
        _empty_lines = 0
        for bline in blines:
            if bline in term._ansicache:
                collines_l = term._ansicache[bline]
                res.extend(collines_l)

                cache_used[bline] = collines_l
            else:
                try:
                    line = bline.decode(ENC, errors='replace')
                    linelen = len(line) + 8*line.count('\t')
                except UnicodeDecodeError as ex:
                    if bline == blines[0]: # string's bytes were cut off => invalid unicode -- skip first line
                        continue
                    else:
                        raise ex

                terminal = AnsiParser(columns=linelen, lines=1, p_in=None)
                terminal.screen.dirty.clear()
                if IS_WIN: # `line` variable contains decoded text, use it
                    bline = bytes(line,'utf-8')
                terminal.feed(bline)
                tiles = terminal.get_line_tiles() # (data, fg, bg, bold, reverse)

                collines_l = []
                nlines = (len(tiles)-1) // self.terminal_w + 1
                for i in range(nlines):
                    line_tiles = tiles[i*self.terminal_w : (i+1)*self.terminal_w]

                    plain_str = ''.join((tile[0] for tile in line_tiles)).rstrip()
                    color_ranges = AnsiParser.get_line_color_ranges(line_tiles)

                    colored_line = (plain_str, color_ranges)
                    res.append(colored_line)
                    collines_l.append(colored_line)

                term._ansicache[bline] = collines_l # for the possibility of repeated lines
                cache_new[bline] = collines_l

        # do not keep in memory lines that are no longer in term.btext
        term._ansicache.clear()
        term._ansicache.update(cache_used)
        term._ansicache.update(cache_new)

        full_text = '\n'.join((item[0] for item in res))

        # for MARKERS_ADD_MANY
        range_lists = {} # (fg,bg,isbold) -> (xs,ys,lens)
        for i,(s,crs) in enumerate(res):
            for cr in crs:
                key = (cr.fgcol, cr.bgcol, cr.isbold)
                xs,ys,lens = range_lists.setdefault(key, ([],[],[]))
                xs.append(cr.start)
                ys.append(i)
                lens.append(cr.length)

        return full_text, range_lists

    def run_cmd(self, text):
        term = self.termbar.get_active_term()
        if not term:
            return

        add_to_history(text, self.max_history_glob)
        term.add_to_history(text)

        self._last_cmd = (text, id(term))

        text = text.lstrip(' ')

        if text==BASH_CLEAR  or  text==CMD_CLEAR:
            term.btext = b''
            #self.memo.set_prop(PROP_RO, False)
            #self.memo.set_text_all('')
            #self.memo.set_prop(PROP_RO, True)
            #return

        self.upd_history_combo()
        self.input.set_text_all('')

        sudo = not IS_WIN and text.startswith('sudo ')
        if sudo:
            text = 'sudo --stdin '+text[5:]

        self._exec(text)

        #sleep(0.05)


    def run_cmd_n(self, n):
        hist = self.get_history_items()

        if n < len(hist):
            s = hist[n]
            self.input.set_text_all(s)
            self.input.set_caret(len(s), 0)

    def recall_cmd(self):
        if not self.is_shown():
            return

        text = self.input.get_text_line(0)
        if text:
            self.ensure_shown()
            recalled = ['{0}\t\t{1}'.format(s, s)  for s in history  if text in s][::-1] # new on top
            self.input.complete_alt('\n'.join(recalled), snippet_id='terminal_pl_recall', len_chars=0)

    def show_history(self):
        hist = self.get_history_items()

        if not hist:
            return

        menu_proc(self.h_menu, MENU_CLEAR)
        for i,item in enumerate(hist):
            menu_proc(self.h_menu, MENU_ADD,
                index=0,
                caption=item,
                command=self.menu_calls[i],
                )

        prop = dlg_proc(self.h_dlg, DLG_CTL_PROP_GET, name='input_parent')
        x, y = prop['x'], prop['y']

        prop = dlg_proc(self.h_dlg, DLG_CTL_PROP_GET, name='panels_parent')
        x_parent, y_parent = prop['x'], prop['y']

        x, y = dlg_proc(self.h_dlg, DLG_COORD_LOCAL_TO_SCREEN, index=x+x_parent, index2=y+y_parent)
        menu_proc(self.h_menu, MENU_SHOW, command=(x, y))

    def upd_history_combo(self):
        hist = self.get_history_items()

        if hist is not None:
            self.input.set_prop(PROP_COMBO_ITEMS, '\n'.join(hist))

    def load_history(self):
        if os.path.exists(fn_history):
            with open(fn_history, 'r', encoding='utf-8') as f:
                lines = [line.rstrip() for line in f.readlines()  if line.rstrip()]
            if lines:
                add_to_history(toadd=lines, maxlen=self.max_history_glob)

    def save_history(self):
        if self.max_history_glob > 0:
            with open(fn_history, 'w', encoding='utf-8') as f:
                f.write('\n'.join(history[-self.max_history_glob:]))

    def get_history_items(self):
        if self.max_history_loc > 0:
            if self.termbar:
                term = self.termbar.get_active_term()
                if term:
                    hist = term.history
                else:
                    hist = [] # clear completion when no terminals
            else:
                hist = None # skip if not initted yet
        else:
            hist = history[-HISTORY_GLOBAL_TAIL_LEN:] # terminal history is off - give last from global history

        return hist

    def queue_focus_input(self, force=False):
        focus_input = lambda tag: (self.input.focus()  if force or self.is_focused() else None)
        timer_proc(TIMER_START_ONE, focus_input, 300, tag='')

    def is_focused(self):
        if not self.h_dlg:
            return False
        p = dlg_proc(self.h_dlg, DLG_PROP_GET)
        return p['focused'] >= 0

    def is_shown(self):
        return self._is_shown  and  (self.floating or app_proc(PROC_SHOW_BOTTOMPANEL_GET, ''))

    def close_all_terms_dlg(self, *args, **vargs):
        self.ensure_shown()
        answer = msg_box(_('Close all terminals?'), MB_OK|MB_OKCANCEL |MB_ICONWARNING)
        if answer == ID_OK:
            self.termbar.close_all()

    def on_statusbar_cell_click(self, id_dlg, id_ctl, data='', info=''):
        self.termbar.on_statusbar_cell_click(id_dlg, id_ctl, data, info)

        if self.termbar.active_term  and  self.termbar.active_term.dirty:
            self.termbar.active_term.dirty = False
            self.termbar.active_term._update_memo_colors()
            self.termbar.active_term._ansicache.clear()
            self.update_output()

    def on_statusbar_menu(self, id_dlg, id_ctl, data='', info=''):
        self.termbar.on_statusbar_menu(id_dlg, id_ctl, data, info)

    def on_statusbar_cell_rename(self, ind_str=''):
        if not self.termbar or not self.termbar.terminals: # Terminal not started
            return
        self.ensure_shown()

        try:
            ind = int(ind_str)
        except ValueError:
            ind = -1 # rename active (menu command)

        self.termbar.run_cmd(CMD_RENAME, ind=ind)

    def on_statusbar_cell_close(self, ind_str):
        try:
            ind = int(ind_str)
        except ValueError:
            pass
        else:
            self.termbar.run_cmd(CMD_CLOSE, ind=ind)

    def on_set_term_icon(self, info):
        ind, icname = info.split(chr(1))
        ind = int(ind)
        self.termbar.set_term_icon(ind=ind, icname=icname)

    def on_set_term_wrap(self, info):
        ind, wrap = info.split(chr(1))
        ind = int(ind)

        if wrap == '':
            wrap = None
        elif wrap == 'custom':
            term = self.termbar.terminals[ind]
            initial_val = term.wrap  if (term.wrap and type(term.wrap) == int) else  80
            answer = dlg_input(_('Wrap margin position:'), str(initial_val))
            try:
                wrap = int(answer)
            except:
                wrap = -1

        if wrap != -1:
            self.termbar.set_term_wrap(ind=ind, wrap=wrap)

    # active editor tab changed
    def on_tab_change(self, ed_self):
        if not self.termbar:
            return

        active_panel = app_proc(PROC_BOTTOMPANEL_GET, '')
        if active_panel not in self.termbar.sidebar_names  and not self.floating:
            return

        self.termbar.on_tab_change()

    def on_tab_move(self, ed_self):
        if self.termbar:
            self.termbar.on_tab_reorder()

    def on_snippet(self, ed_self, snippet_id, snippet_text):
        if not self.h_dlg:
            return

        if self.input == ed_self and snippet_id == 'terminal_pl_recall':
            self.input.set_text_all(snippet_text)
            self.input.set_caret(len(snippet_text), 0)

    def form_key_down(self, id_dlg, id_ctl, data='', info=''):
        #Enter
        if (id_ctl==keys.VK_ENTER) and (data==''):
            text = self.input.get_text_line(0)
            self.input.set_text_all('')
            self.input.set_caret(0, 0)
            self.run_cmd(text)
            return False

        #Up/Down: scroll memo
        elif (id_ctl==keys.VK_UP) and (data==''):
            if self.memo:
                self.memo.cmd(cmds.cCommand_ScrollLineUp)
            return False

        elif (id_ctl==keys.VK_DOWN) and (data==''):
            if self.memo and self.termbar and self.termbar.active_term:
                y,maxy = self.termbar.active_term.get_memo_sroll_vert()
                if y >= maxy: # memo at the bottom - send arrow down
                    self.termbar.active_term.ch_out.write(TERM_KEY_DOWN)
                else:
                    self.memo.cmd(cmds.cCommand_ScrollLineDown)
            return False

        #PageUp/PageDown: scroll memo
        elif (id_ctl==keys.VK_PAGEUP) and (data==''):
            if self.memo:
                self.memo.cmd(cmds.cCommand_ScrollPageUp)
            return False

        elif (id_ctl==keys.VK_PAGEDOWN) and (data==''):
            if self.memo and self.termbar and self.termbar.active_term:
                y,maxy = self.termbar.active_term.get_memo_sroll_vert()
                if y >= maxy: # memo at the bottom - send page down
                    self.termbar.active_term.ch_out.write(TERM_KEY_PAGE_DOWN)
                else:
                    self.memo.cmd(cmds.cCommand_ScrollPageDown)
            return False

        #Ctrl+Down: history menu
        elif (id_ctl==keys.VK_DOWN) and (data=='c'):
            self.show_history()
            return False

        #Escape: go to editor
        elif (id_ctl==keys.VK_ESCAPE) and (data==''):
            # Stops the timer
            timer_proc(TIMER_STOP, self.timer_update, 0)
            ed.focus()
            if not self.floating:
                ed.cmd(cmds.cmd_ToggleBottomPanel)
            return False

        #Break or Ctrl+C (cannot react to Ctrl+Break)
        elif (id_ctl==keys.VK_PAUSE) or (id_ctl==ord('C') and data=='c'):
            self.button_break_click(0, 0)
            return False

        #Ctrl+R: recall commands from global history
        elif id_ctl == ord('R')  and data == 'c':
            self.recall_cmd()
            return False

        #Alt+Down/Up: cucle through history
        elif id_ctl in [keys.VK_UP, keys.VK_DOWN]  and  data == 'a':
            hist = self.get_history_items()
            if not hist:
                return False
            txt = self.input.get_text_all()
            _is_up = (id_ctl == keys.VK_UP)

            try:
                _ind = hist.index(txt)
                _ind_new = _ind+1  if _is_up else  _ind-1
                if _ind_new < 0:             new_txt = txt
                elif _ind_new >= len(hist):  new_txt = hist[-1]
                else:                        new_txt = hist[_ind_new]
            except ValueError:
                new_txt = ''  if _is_up else  hist[-1]

            self.input.set_text_all(new_txt)
            self.input.set_caret(0,0, len(new_txt),0)

            return False

        #elif (id_ctl==keys.VK_RIGHT) and (data=='s'):
        #elif (id_ctl==keys.VK_LEFT) and (data=='s'):
        # _dbg_toggle_term_hide...

    def form_hide(self, id_dlg, id_ctl, data='', info=''):
        timer_proc(TIMER_STOP, self.timer_update, 0)

        self._is_shown = False

    def form_show(self, id_dlg, id_ctl, data='', info=''):
        term_name = app_proc(PROC_BOTTOMPANEL_GET, "")

        log('* on_show, cur panel: ' + str(term_name))

        if self.termbar:
            self.termbar.show_terminal(name=term_name)
        self.queue_focus_input(force=True)

        timer_proc(TIMER_START, self.timer_update, 300, tag='')

        self._is_shown = True

    def on_state(self, ed, state):
        if self.h_dlg and state == APPSTATE_THEME_UI:
            colors = self._get_theme_colors()
            update_memos = self._load_term_theme()

            dlg_proc(self.h_embed, DLG_CTL_PROP_SET, name='panel_placeholder', prop={
                'color': self.color_btn_back,
                'font_color': self.color_btn_font,
            })
            dlg_proc(self.h_dlg, DLG_PROP_SET, prop={
                'color': self.color_btn_back,
            })
            dlg_proc(self.h_dlg, DLG_CTL_PROP_SET, name='input', prop={
                'color': self.color_btn_back,
            })

            for name,val in globals().items():
                if name.startswith('COLOR_ID_') and type(val) == str:
                    theme_item_name = val
                    theme_item = colors.get(theme_item_name)
                    if theme_item is not None:
                        theme_col = theme_item['color']
                        self.input.set_prop(PROP_COLOR, (theme_item_name, theme_col))

            if self.termbar:
                self.termbar.on_theme_change(update_terminals=update_memos)
                if update_memos  and  self.termbar.active_term  and  self.termbar.active_term.btext:
                    self.termbar.active_term.dirty = False
                    self.termbar.active_term._update_memo_colors()
                    self.termbar.active_term._ansicache.clear()
                    self.update_output()

    def on_exit(self, ed_self):
        self._save_state()
        self._save_pos()
        self.save_history()

        timer_proc(TIMER_STOP, self.timer_update, 0)

        self.termbar.on_exit()


    def button_break_click(self, id_dlg, id_ctl, data='', info=''):
        if self.termbar:
            term = self.termbar.get_active_term()
            if term:
                term.restart_shell()
            self.queue_focus_input()

    def cmd_new_term(self):
        self.ensure_shown()
        filepath = ed.get_filename()
        self.termbar.new_term(filepath=filepath)

    def cmd_new_term_nofile(self):
        self.ensure_shown()
        self.termbar.new_term(filepath='')

    def cmd_close_last_cur(self):
        if not self.is_shown(): # ignore if terminal panel is closed
            return
        self.termbar.run_cmd(CMD_CLOSE_LAST_CUR_FILE)

    def cmd_close_cur_term(self):
        if not self.is_shown(): # ignore if terminal panel is closed
            return
        self.termbar.run_cmd(CMD_CLOSE)

    def cmd_cur_file_term_switch(self):
        is_ed_focused = ed.get_prop(PROP_FOCUSED)
        if not self.h_dlg:
            self.open()
        self.termbar.run_cmd(CMD_CUR_FILE_TERM_SWITCH, is_ed_focused=is_ed_focused)

    def cmd_next(self):
        self.ensure_shown()
        self.termbar.run_cmd(CMD_NEXT)

    def cmd_previous(self):
        self.ensure_shown()
        self.termbar.run_cmd(CMD_PREVIOUS)

    def cmd_exec_selected(self):
        if self.is_shown(): # only when visible
            self.termbar.run_cmd(CMD_EXEC_SEL)

    def cmd_repeat_last(self):
        if not self._last_cmd:
            return
        last_cmd, term_id = self._last_cmd
        term = next((t for t in self.termbar.terminals  if id(t) == term_id), None)
        if not term  or  not term.memo:
            return  msg_status(_('Last command terminal is no longer present'))

        _msg = _('Execute last command?') + f' - {term.name!r}\n\n> {last_cmd}'
        _answer = msg_box(_msg, MB_OK|MB_OKCANCEL |MB_ICONQUESTION)
        if _answer != ID_OK:
            return

        ind = self.termbar.terminals.index(term)
        self.termbar._show_terminal(ind=ind, focus_input=False)
        self.run_cmd(last_cmd)

        msg_status(_('Executed: ') + last_cmd)

class AnsiParser:
    def __init__(self, columns, lines, p_in):
        '!!! investigate init'
        self.screen = HistoryScreen(columns, lines)
        self.screen.set_mode(pyte.modes.LNM)
        self.screen.write_process_input = lambda data: p_in.write(data.encode())

        #self.stream = pyte.ByteStream()
        self.stream = ByteStream()
        self.stream.attach(self.screen)

    def feed(self, data):
        self.stream.feed(data)

    # char:  bg, bold, count, data, fg, index, italics, reverse, strikethrough, underscore
    def get_indexed_lines(self, ):
        cursor = self.screen.cursor
        lines = []
        for y in self.screen.dirty:
            line = self.screen.buffer[y]
            data = [(char.data, char.fg, char.bg, char.bold, char.reverse)
                    for char in (line[x] for x in range(self.screen.columns))]
            lines.append((y, data))

        self.screen.dirty.clear()
        return lines

    def get_line_tiles(self):
        #tiles = [(char.data, char.fg, char.bg, char.bold, char.reverse)
                    #for char in self.screen.buffer[0].values()]
        tiles = []
        last_i = -1
        for i,char in self.screen.buffer[0].items():
            if i - last_i > 1:   # missing tiles - tab character
                delta = i-last_i
                tabs =  (i-last_i-2)//8+1
                tiles.extend(('\t', 'default', 'default', False, False)  for i in range(tabs))
            tiles.append((char.data, char.fg, char.bg, char.bold, char.reverse))
            last_i = i

        return tiles

    def get_line_color_ranges(tiles):
        range_start = -1
        fg = DEFAULT_FGCOL
        bg = DEFAULT_BGCOL
        isbold = False
        l = []
        for i,(ch, tfg, tbg, tbold, treverse) in enumerate(tiles):
            if fg != tfg  or bg != tbg  or tbold != isbold: # new color range
                # finish previous color range
                if fg != DEFAULT_FGCOL  or bg != DEFAULT_BGCOL  or isbold: # wasnt plain text-range
                    l.append(ColorRange(start=range_start, length=i-range_start, fgcol=fg, bgcol=bg, isbold=isbold))
                range_start = i
                fg = tfg
                bg = tbg
                isbold = tbold
        # add last range  if any
        if fg != DEFAULT_FGCOL  or bg != DEFAULT_BGCOL  or isbold:
            l.append(ColorRange(start=range_start, length=len(tiles)-range_start, fgcol=fg, bgcol=bg, isbold=isbold))
        return l

