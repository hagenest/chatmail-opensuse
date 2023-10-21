import os
import io
import time
import random
import subprocess
import imaplib
import smtplib
import itertools
from email.parser import BytesParser
from email import policy
from pathlib import Path
from math import ceil
import pytest


conftestdir = Path(__file__).parent


def pytest_addoption(parser):
    parser.addoption(
        "--slow", action="store_true", default=False, help="also run slow tests"
    )


def pytest_configure(config):
    config._benchresults = {}
    config.addinivalue_line(
        "markers", "slow: mark test to require --slow option to run"
    )


def pytest_runtest_setup(item):
    markers = list(item.iter_markers(name="slow"))
    if markers:
        if not item.config.getoption("--slow"):
            pytest.skip("skipping slow test, use --slow to run")


@pytest.fixture
def maildomain():
    domain = os.environ.get("CHATMAIL_DOMAIN")
    if not domain:
        pytest.skip("set CHATMAIL_DOMAIN to a ssh-reachable chatmail instance")
    return domain


@pytest.fixture
def sshdomain(maildomain):
    return os.environ.get("CHATMAIL_SSH", maildomain)


@pytest.fixture
def maildomain2():
    domain = os.environ.get("CHATMAIL_DOMAIN2")
    if not domain:
        pytest.skip("set CHATMAIL_DOMAIN2 to a ssh-reachable chatmail instance")
    return domain


@pytest.fixture
def sshdomain2(maildomain2):
    return os.environ.get("CHATMAIL_SSH2", maildomain2)


def pytest_report_header():
    domain = os.environ.get("CHATMAIL_DOMAIN")
    if domain:
        text = f"chatmail test instance: {domain}"
        return ["-" * len(text), text, "-" * len(text)]


@pytest.fixture
def benchmark(request):
    def bench(func, num, name=None):
        if name is None:
            name = func.__name__
        durations = []
        for i in range(num):
            now = time.time()
            func()
            durations.append(time.time() - now)
        durations.sort()
        request.config._benchresults[name] = durations

    return bench


def pytest_terminal_summary(terminalreporter):
    tr = terminalreporter
    results = tr.config._benchresults
    if not results:
        return

    tr.section("benchmark results")
    float_names = "median min max".split()
    width = max(map(len, float_names))

    def fcol(parts):
        return " ".join(part.rjust(width) for part in parts)

    headers = f"{'benchmark name': <30} " + fcol(float_names)
    tr.write_line(headers)
    tr.write_line("-" * len(headers))
    for name, durations in results.items():
        measures = [
            sorted(durations)[len(durations) // 2],
            min(durations),
            max(durations),
        ]
        line = f"{name: <30} "
        line += fcol(f"{float: 2.2f}" for float in measures)
        tr.write_line(line)


@pytest.fixture
def imap(maildomain):
    return ImapConn(maildomain)


class ImapConn:
    AuthError = imaplib.IMAP4.error
    logcmd = "journalctl -f -u dovecot"
    name = "dovecot"

    def __init__(self, host):
        self.host = host

    def connect(self):
        print(f"imap-connect {self.host}")
        self.conn = imaplib.IMAP4_SSL(self.host)

    def login(self, user, password):
        print(f"imap-login {user!r} {password!r}")
        self.conn.login(user, password)

    def fetch_all(self):
        print("imap-fetch all")
        status, res = self.conn.select()
        if int(res[0]) == 0:
            raise ValueError("no messages in imap folder")
        status, results = self.conn.fetch("1:*", "(RFC822)")
        assert status == "OK"
        return results

    def fetch_all_messages(self):
        print("imap-fetch all messages")
        results = self.fetch_all()
        messages = []
        for item in results:
            if len(item) == 2:
                messages.append(item[1].decode())
        return messages


@pytest.fixture
def smtp(maildomain):
    return SmtpConn(maildomain)


class SmtpConn:
    AuthError = smtplib.SMTPAuthenticationError
    logcmd = "journalctl -f -t postfix/smtpd -t postfix/smtp -t postfix/lmtp"
    name = "postfix"

    def __init__(self, host):
        self.host = host

    def connect(self):
        print(f"smtp-connect {self.host}")
        self.conn = smtplib.SMTP_SSL(self.host)

    def login(self, user, password):
        print(f"smtp-login {user!r} {password!r}")
        self.conn.login(user, password)

    def sendmail(self, from_addr, to_addrs, msg):
        print(f"smtp-sendmail from={from_addr!r} to_addrs={to_addrs!r}")
        print(f"smtp-sendmail message size: {len(msg)}")
        return self.conn.sendmail(from_addr=from_addr, to_addrs=to_addrs, msg=msg)


@pytest.fixture(params=["imap", "smtp"])
def imap_or_smtp(request):
    return request.getfixturevalue(request.param)


@pytest.fixture
def gencreds(maildomain):
    count = itertools.count()
    next(count)

    def gen(domain=None):
        domain = domain if domain else maildomain
        while 1:
            num = next(count)
            alphanumeric = "abcdefghijklmnopqrstuvwxyz1234567890"
            user = "".join(random.choices(alphanumeric, k=10))
            user = f"ac{num}_{user}"
            password = "".join(random.choices(alphanumeric, k=10))
            yield f"{user}@{domain}", f"{password}"

    return lambda domain=None: next(gen(domain))


#
# Delta Chat testplugin re-use
# use the cmfactory fixture to get chatmail instance accounts
#


class ChatmailTestProcess:
    """Provider for chatmail instance accounts as used by deltachat.testplugin.acfactory"""

    def __init__(self, pytestconfig, maildomain, gencreds):
        self.pytestconfig = pytestconfig
        self.maildomain = maildomain
        assert "." in self.maildomain, maildomain
        self.gencreds = gencreds
        self._addr2files = {}

    def get_liveconfig_producer(self):
        while 1:
            user, password = self.gencreds(self.maildomain)
            config = {
                "addr": user,
                "mail_pw": password,
            }
            # speed up account configuration
            config["mail_server"] = self.maildomain
            config["send_server"] = self.maildomain
            yield config

    def cache_maybe_retrieve_configured_db_files(self, cache_addr, db_target_path):
        pass

    def cache_maybe_store_configured_db_files(self, acc):
        pass


@pytest.fixture
def cmfactory(request, gencreds, tmpdir, data, maildomain):
    # cloned from deltachat.testplugin.amfactory
    pytest.importorskip("deltachat")
    from deltachat.testplugin import ACFactory

    testproc = ChatmailTestProcess(request.config, maildomain, gencreds)
    am = ACFactory(request=request, tmpdir=tmpdir, testprocess=testproc, data=data)

    # nb. a bit hacky
    # would probably be better if deltachat's test machinery grows native support
    def switch_maildomain(maildomain2):
        am.testprocess.maildomain = maildomain2

    am.switch_maildomain = switch_maildomain

    yield am
    if hasattr(request.node, "rep_call") and request.node.rep_call.failed:
        if testproc.pytestconfig.getoption("--extra-info"):
            logfile = io.StringIO()
            am.dump_imap_summary(logfile=logfile)
            print(logfile.getvalue())
            # request.node.add_report_section("call", "imap-server-state", s)


@pytest.fixture
def remote(sshdomain):
    return Remote(sshdomain)


class Remote:
    def __init__(self, sshdomain):
        self.sshdomain = sshdomain

    def iter_output(self, logcmd=""):
        getjournal = f"journalctl -f" if not logcmd else logcmd
        self.popen = subprocess.Popen(
            ["ssh", f"root@{self.sshdomain}", getjournal],
            stdout=subprocess.PIPE,
        )
        while 1:
            line = self.popen.stdout.readline()
            res = line.decode().strip().lower()
            if res:
                yield res
            else:
                break


@pytest.fixture
def maildata(request, gencreds):
    datadir = conftestdir.joinpath("mail-data")

    def maildata(name, parsed=True, from_addr=None, to_addr=None):
        if from_addr is None:
            from_addr = gencreds()[0]
        if to_addr is None:
            to_addr = gencreds()[0]
        data = datadir.joinpath(name).read_text()
        text = data.format(from_addr=from_addr, to_addr=to_addr)
        return BytesParser(policy=policy.default).parsebytes(text.encode())

    return maildata


@pytest.fixture
def cmsetup(maildomain, gencreds):
    return CMSetup(maildomain, gencreds)


class CMSetup:
    def __init__(self, maildomain, gencreds):
        self.maildomain = maildomain
        self.gencreds = gencreds

    def gen_users(self, num):
        print(f"Creating {num} online users")
        users = []
        for i in range(num):
            addr, password = self.gencreds()
            user = CMUser(self.maildomain, addr, password)
            assert user.smtp
            users.append(user)
        return users


class CMUser:
    def __init__(self, maildomain, addr, password):
        self.maildomain = maildomain
        self.addr = addr
        self.password = password
        self._smtp = None
        self._imap = None

    @property
    def smtp(self):
        if not self._smtp:
            handle = SmtpConn(self.maildomain)
            handle.connect()
            handle.login(self.addr, self.password)
            self._smtp = handle
        return self._smtp

    @property
    def imap(self):
        if not self._imap:
            imap = ImapConn(self.maildomain)
            imap.connect()
            imap.login(self.addr, self.password)
            self._imap = imap
        return self._imap