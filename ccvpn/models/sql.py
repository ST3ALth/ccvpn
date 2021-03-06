from sqlalchemy import (
    Column, ForeignKey, Enum,
    Integer, Float, DateTime, Boolean, BigInteger,
    String, Text, LargeBinary, Interval,
    func
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.sql.expression import true, false
from datetime import datetime, timedelta
import random
import re
import hashlib

from .base import Base, DBSession
from .types import MutableDict, JSONEncodedDict


prng = random.SystemRandom()


def random_access_token():
    charset = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    base = len(charset)
    return ''.join([charset[prng.randint(0, base - 1)] for n in range(32)])


def random_gift_code():
    charset = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    base = len(charset)
    return ''.join([charset[prng.randint(0, base - 1)] for n in range(16)])


def random_bytes(length):
    return bytearray([prng.randint(0, 0xff) for n in range(length)])


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, doc='ID')
    username = Column(String(length=32), unique=True, nullable=False,
                      doc='Username')
    password = Column(LargeBinary(length=96), nullable=False, doc='Password')
    email = Column(String(length=256), nullable=True, default=None,
                   doc='E-mail')
    is_active = Column(Boolean, nullable=False, default=True, doc='Active?')
    is_admin = Column(Boolean, nullable=False, default=False, doc='Admin?')
    month_bw = Column(BigInteger, nullable=False, default=0)
    signup_date = Column(DateTime, nullable=False, default=datetime.now)
    last_login = Column(DateTime, nullable=True, default=None)
    paid_until = Column(DateTime, nullable=True, default=None)
    last_expiry_notice = Column(DateTime, nullable=True, default=None)
    referrer_id = Column(ForeignKey('users.id'), nullable=True)

    giftcodes_used = relationship('GiftCode', backref='user')
    orders = relationship('Order', backref='user')
    paid_orders = relationship('Order', viewonly=True,
                               primaryjoin='and_(Order.uid == User.id, Order.paid == True)')
    profiles = relationship('Profile', backref='user', order_by='Profile.name')
    pw_reset_tokens = relationship('PasswordResetToken', backref='user')
    sessions = relationship('VPNSession', backref='user', lazy='dynamic')

    tickets = relationship('Ticket', backref='user')
    ticketmessages = relationship('TicketMessage', backref='user')

    username_re = re.compile('^[a-zA-Z0-9_-]{2,32}$')
    email_re = re.compile('^.+@.+$')

    def __init__(self, *args, **kwargs):
        password = kwargs.pop('password', None)
        if password:
            self.set_password(password)
        super().__init__(*args, **kwargs)

    @hybrid_property
    def is_support(self):
        return self.is_admin

    @hybrid_property
    def is_paid(self):
        return self.paid_until is not None and self.paid_until > datetime.now()

    def add_paid_time(self, time):
        new_date = self.paid_until
        if not self.is_paid:
            new_date = datetime.now()
        try:
            new_date += time
        except OverflowError:
            return
        self.paid_until = new_date

    @hybrid_property
    def paid_time_left(self):
        if self.is_paid:
            return self.paid_until - datetime.now()
        else:
            return timedelta()

    @paid_time_left.expression
    def paid_time_left(cls):
        return cls.paid_until - func.now()

    def paid_days_left(self):
        if self.is_paid:
            time = self.paid_until - datetime.now()
            days = time.days + (time.seconds / 60 / 60 / 24)
            return max(1, round(days))
        else:
            return 0

    @classmethod
    def validate_username(cls, username):
        return username and cls.username_re.match(username)

    @classmethod
    def validate_email(cls, email):
        return email and cls.email_re.match(email) and len(email) <= 256

    @classmethod
    def validate_password(self, clearpw):
        return clearpw and 0 < len(clearpw) < 256

    def set_password(self, clearpw):
        salt = random_bytes(32)
        password = bytearray(clearpw, 'utf-8')
        hash = hashlib.sha512(salt + password).digest()
        self.password = salt + hash
        return True

    def check_password(self, clearpw):
        if not self.password or len(self.password) != 96:
            return False
        salt = self.password[:32]
        password = bytearray(clearpw, 'utf-8')
        hash = hashlib.sha512(salt + password).digest()
        return self.password[32:96] == hash

    def __str__(self):
        return self.username

    def __repr__(self):
        return '<User #%s \'%s\'>' % (self.id, self.username)

    @classmethod
    def is_used(cls, username, email):
        nc = DBSession.query(func.count(User.id).label('nc')) \
            .filter(func.lower(User.username) == username.lower()) \
            .subquery()
        ec = DBSession.query(func.count(User.id).label('ec')) \
            .filter_by(email=email) \
            .subquery()
        c = DBSession.query(nc, ec).first()
        return (c.nc, c.ec)


class PasswordResetToken(Base):
    __tablename__ = 'pwresettoken'
    id = Column(Integer, primary_key=True)
    uid = Column(ForeignKey('users.id'), nullable=False)
    token = Column(String(32), nullable=False)
    expire_date = Column(DateTime, nullable=True)

    def __init__(self, uid, token=None, expire=None):
        if isinstance(uid, User):
            uid = uid.id

        default_ttl = timedelta(days=2)

        self.uid = uid
        self.token = token or random_access_token()
        self.expire_date = expire or datetime.now() + default_ttl


class Profile(Base):
    """ Profile.

    - name: used to display profile and for VPN auth.
      The "default" profile (created for every user) has name="".
    - gateway_country/gateway_id: used to filter gateways.
      both None: random.
    """

    PROTOCOLS = {
        'udp': 'UDP (default)',
        'tcp': 'TCP',
        'udpl': 'UDP (low MTU)',
    }

    CLIENT_OS = {
        'windows': 'Windows',
        'android': 'Android',
        'ubuntu': 'Ubuntu',
        'osx': 'OS X',
        'freebox': 'Freebox',
        'other': 'Other / GNU/Linux',
    }

    __tablename__ = 'profiles'
    id = Column(Integer, primary_key=True)
    uid = Column(ForeignKey('users.id'))
    name = Column(String(16), nullable=False)
    password = Column(Text, nullable=True)

    # Gateway selection
    gateway_country = Column(String, nullable=True)
    gateway_id = Column(ForeignKey('gateways.id'), nullable=True)

    # OpenVPN config settings
    protocol = Column(Enum(*PROTOCOLS.keys(), name='protocols_enum'),
                      nullable=False, default='udp')
    client_os = Column(Enum(*CLIENT_OS.keys(), name='client_os_enum'),
                       nullable=True)
    use_http_proxy = Column(String, nullable=True)
    disable_ipv6 = Column(Boolean, nullable=False, default=False)

    sessions = relationship('VPNSession', backref='profile')

    def validate_name(self, name):
        return re.match('^[a-zA-Z0-9]{1,16}$', name)

    @property
    def vpn_username(self):
        if self.name:
            return self.user.username + '/' + self.name
        else:
            return self.user.username

    def get_vpn_remote_host(self, domain):
        if self.gateway_id:
            name = self.gateway.country + '-' + self.gateway.name
        elif self.gateway_country:
            name = self.gateway_country
        else:
            name = 'random'
        return 'gw.' + name + domain

    def get_vpn_remote(self, domain):
        openvpn_proto = {'udp': 'udp', 'udpl': 'udp', 'tcp': 'tcp'}
        openvpn_ports = {'udp': 1196,  'udpl': 1194,  'tcp': 443}

        # remote <host> <port> <proto>
        remote = self.get_vpn_remote_host(domain)
        remote += ' ' + str(openvpn_ports[self.protocol])
        remote += ' ' + openvpn_proto[self.protocol]
        return remote


class AlreadyUsedGiftCode(Exception):
    pass


class GiftCode(Base):
    __tablename__ = 'giftcodes'
    id = Column(Integer, primary_key=True, nullable=False, doc='ID')
    code = Column(String(16), unique=True, nullable=False, doc='Code',
                  default=random_gift_code)
    time = Column(Interval, default=timedelta(days=30), nullable=False,
                  doc='Time')
    free_only = Column(Boolean, default=False, nullable=False,
                       server_default=false())
    used = Column(ForeignKey('users.id'), nullable=True)

    def __init__(self, time=None, code=None, used=None):
        if isinstance(used, User):
            used = used.id

        self.time = time or timedelta(days=30)
        self.used = used
        self.code = code or random_gift_code()

    @property
    def username_if_used(self):
        '''User'''
        if self.used and self.user:
            return self.user.username
        else:
            return False

    def use(self, user, reuse=False):
        """Use this GiftCode on user

        :param user: User
        :param reuse: bool allow to reuse a code?

        """

        if self.used and not reuse:
            raise AlreadyUsedGiftCode()
        if self.free_only and user.is_paid:
            raise AlreadyUsedGiftCode()
        self.used = user.id
        user.add_paid_time(self.time)

    def __str__(self):
        return self.code


class OrderNotPaid(Exception):
    pass


class Order(Base):
    """ Order

    close_date: Expiration date of the order, mainly used to not display old
        started orders.
        NOT the date when the order is closed. (FIXME)
    """

    __tablename__ = 'orders'

    class METHOD:
        BITCOIN = 0
        PAYPAL = 1
        STRIPE = 2

    id = Column(Integer, primary_key=True)
    uid = Column(ForeignKey('users.id'))
    start_date = Column(DateTime, nullable=False, default=datetime.now)
    close_date = Column(DateTime, nullable=True)
    amount = Column(Float, nullable=False)
    paid_amount = Column(Float, nullable=False, default=0)
    time = Column(Interval, nullable=True)
    method = Column(Integer, nullable=False)
    paid = Column(Boolean, nullable=False, default=False)
    payment = Column(MutableDict.as_mutable(JSONEncodedDict), nullable=True)

    @property
    def currency(self):
        # TODO: use method instead
        if self.method == self.METHOD.BITCOIN:
            return 'BTC'
        if self.method == self.METHOD.PAYPAL:
            return '€'
        if self.method == self.METHOD.STRIPE:
            return '€'

    def is_paid(self):
        """ Check if the order has been paid, but should not be used instead
        of the paid column, that stores the status of the order.
        If an order has paid=False and is_paid() == True, it should not be
        displayed as paid, and is still waiting to be processed.

        TODO: Remove the paid column if it's not really useful and can lead to
              database inconsistency (paid / is_paid())
        """
        return self.paid_amount >= self.amount

    def close(self, force=False):
        """Close a paid order.

        :raises: OrderNotPaid
        """
        if not self.is_paid() and not force:
            raise OrderNotPaid(self)

        self.paid = True
        self.user.add_paid_time(self.time)

        if self.user.referrer_id:
            try:
                referrer = User.one(id=self.user.referrer_id)
                referrer.add_paid_time(timedelta(days=14))
            except NoResultFound:
                pass
            # Given 14d or inexistent, we can remove it.
            self.user.referrer_id = None

    def __init__(self, user, amount, time, method, paid_amount=0, ttl=None):
        self.uid = user.id if isinstance(user, User) else user
        self.amount = amount
        self.time = time
        self.paid_amount = paid_amount
        self.paid = paid_amount >= amount
        self.payment = {}
        self.method = method

        ttl = ttl or timedelta(days=30)

        self.start_date = datetime.now()
        self.close_date = datetime.now() + ttl

    def __str__(self):
        return hex(self.id)[2:]

    def __repr__(self):
        return '<Order %d by %s, %s>' % (self.id, self.user.username,
                                         self.time)


class Gateway(Base):
    __tablename__ = 'gateways'
    id = Column(Integer, primary_key=True)
    name = Column(String(32), nullable=False)
    isp_name = Column(String(32), nullable=False)
    isp_url = Column(String, nullable=False)
    country = Column(String(2), nullable=False)
    token = Column(String(32), nullable=False, default=random_access_token)
    ipv4 = Column(String, nullable=True)
    ipv6 = Column(String, nullable=True)
    bps = Column(BigInteger, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True,
                     server_default=true())

    sessions = relationship('VPNSession', backref='gateway')
    profiles = relationship('Profile', backref='gateway')

    @property
    def main_ip4(self):
        return self.ipv4

    def __repr__(self):
        return '<Gateway %s-%s>' % (self.country, self.name)

    def __str__(self):
        return self.country + '-' + self.name


class VPNSession(Base):
    __tablename__ = 'vpnsessions'
    id = Column(Integer, primary_key=True)
    gateway_id = Column(ForeignKey('gateways.id'), nullable=False)
    gateway_version = Column(Integer, nullable=False)
    user_id = Column(ForeignKey('users.id'), nullable=False)
    profile_id = Column(ForeignKey('profiles.id'), nullable=True)
    connect_date = Column(DateTime, default=datetime.now, nullable=False)
    disconnect_date = Column(DateTime, nullable=True)
    remote_addr = Column(String, nullable=False)
    internal_ip4 = Column(String, nullable=True)
    internal_ip6 = Column(String, nullable=True)
    bytes_up = Column(BigInteger, nullable=True)
    bytes_down = Column(BigInteger, nullable=True)

    @hybrid_property
    def is_online(self):
        return self.disconnect_date == None

    @property
    def duration(self):
        if self.connect_date and self.disconnect_date:
            return self.disconnect_date - self.connect_date
        return None

    def __repr__(self):
        return '<VPNSession %d gw %d %s user %d, %s -> %s>' % (
            self.id, self.gateway_id, self.gateway_version, self.user_id,
            self.connect_date, self.disconnect_date)


class Ticket(Base):
    __tablename__ = 'tickets'
    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey('users.id'), nullable=False)
    create_date = Column(DateTime, default=datetime.now, nullable=False)
    closed = Column(Boolean, default=False, nullable=False)
    close_date = Column(DateTime, nullable=True)
    subject = Column(String, nullable=False)
    notify_owner = Column(Boolean, default=False, nullable=False)

    messages = relationship('TicketMessage', backref='ticket', order_by='TicketMessage.create_date')

    def close(self):
        """ Close ticket and update close date """
        self.closed = True
        self.close_date = datetime.now()


class TicketMessage(Base):
    __tablename__ = 'ticket_messages'
    id = Column(Integer, primary_key=True)
    ticket_id = Column(ForeignKey('tickets.id'), nullable=False)
    user_id = Column(ForeignKey('users.id'), nullable=False)
    create_date = Column(DateTime, default=datetime.now, nullable=False)
    content = Column(Text, nullable=False)


def get_user(request):
    if 'uid' not in request.session:
        return None

    uid = request.session['uid']
    user = DBSession.query(User).filter_by(id=uid).first()
    if not user:
        del request.session['uid']
        return None
    return user

