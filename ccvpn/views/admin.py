from pyramid.response import Response
from pyramid.view import view_config
from pyramid.httpexceptions import HTTPSeeOther, HTTPBadRequest, HTTPNotFound
from pyramid.renderers import render_to_response
from datetime import datetime, timedelta, date
from ccvpn.models import DBSession, User, Order, GiftCode, Gateway

from dateutil import parser


def monthdelta(date, delta):
    m = (date.month + delta) % 12
    y = date.year + (date.month + delta - 1) // 12
    if not m:
        m = 12
    d = min(date.day, [31, 29 if y % 4 == 0 and not y % 400 == 0
                       else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31
                       ][m - 1])
    return date.replace(day=d, month=m, year=y)


def last_days(n=30):
    now = date.today()
    for i in range(n - 1, -1, -1):
        yield now - timedelta(days=i)


def last_months(n=12):
    now = date.today().replace(day=1)
    for i in range(n - 1, -1, -1):
        yield monthdelta(now, -i)


def time_filter(period, m, df):
    def _filter(o):
        if period == 'm':
            return df(o).date() == m
        if period == 'y':
            return df(o).date().replace(day=1) == m
    return _filter


def time_filter_future(period, m, df):
    def _filter(o):
        if period == 'm':
            return df(o).date() <= m
        if period == 'y':
            return df(o).date().replace(day=1) <= m
    return _filter


@view_config(route_name='admin_graph', permission='admin')
def admin_graph(request):
    graph_name = request.matchdict['name']

    try:
        import pygal
    except ImportError:
        raise HTTPNotFound()

    def get(name, default=None, type=str):
        try:
            return type(request.GET.get(name, default))
        except ValueError:
            raise HTTPBadRequest()

    pygalopts = {
        'js': [
            request.static_url('ccvpn:static/pygal/svg.jquery.js'),
            request.static_url('ccvpn:static/pygal/pygal-tooltips.js')
        ]
    }

    period = get('period', 'm')
    if period == 'm':
        period_time = timedelta(days=30)
    if period == 'y':
        period_time = timedelta(days=365)

    if graph_name == 'users':
        period = get('period', 'm')

        chart = pygal.Line(fill=True, x_label_rotation=75, show_legend=False,
                           **pygalopts)
        chart.title = 'Users (%s)' % period
        chart.x_labels = []
        values = []
        gen = last_days(30) if period == 'm' else last_months(12)
        users = DBSession.query(User).all()

        for m in gen:
            filter_ = time_filter_future(period, m, lambda o: o.signup_date)
            users_filtered = filter(filter_, users)
            values.append(len(list(users_filtered)))
            chart.x_labels.append('%s/%s/%s' % (m.year, m.month, m.day))

        chart.add('Users', values)
        return Response(chart.render(), content_type='image/svg+xml')

    elif graph_name == 'income':
        method = get('method', 0, int)
        if not method in request.payment_methods:
            raise HTTPNotFound()
        method_name = request.payment_methods[method].name

        chart = pygal.StackedBar(x_label_rotation=75, show_legend=True,
                                 **pygalopts)
        chart.title = 'Income (%s, %s)' % (method_name, period)
        orders = DBSession.query(Order) \
            .filter(Order.start_date > datetime.now() - period_time) \
            .filter(Order.method == method) \
            .filter(Order.paid == True) \
            .all()

        # Prepare value dict
        values = {}
        for order in orders:
            t = order.time
            if t not in values:
                values[t] = []

        chart.x_labels = []
        gen = last_days(30) if period == 'm' else last_months(12)
        for m in gen:
            filter_ = time_filter(period, m, lambda o: o.start_date)
            orders_date = list(filter(filter_, orders))

            for duration in values.keys():
                filter_ = lambda o: o.time == duration
                orders_dd = list(filter(filter_, orders_date))

                sum_ = sum(o.paid_amount for o in orders_dd)
                values[duration].append(round(sum_, 4) or None)

            chart.x_labels.append('%s' % m)

        for time, v in values.items():
            label = '%sd' % time.days
            chart.add(label, v)
        return Response(chart.render(), content_type='image/svg+xml')
    else:
        raise HTTPNotFound()


@view_config(route_name='admin_home', renderer='admin/home.mako',
             permission='admin')
def admin_home(request):
    try:
        import pygal  # noqa
        graph = True
    except ImportError as e:
        request.session.flash(('error', 'Pygal not found: cannot make charts'))
        graph = False

    btcm = request.payment_methods['bitcoin']
    btcrpc = btcm.rpc
    try:
        btcd = btcrpc.getinfo()
    except (ValueError, ConnectionRefusedError):
        btcd = None
    return {'graph': graph, 'btcd': btcd}


class AdminView(object):
    ''' Basic CRUD view for admin stuff '''
    model = None
    item_template = None
    list_template = None

    def __init__(self, request):
        self.request = request

    def tvars(self, d):
        d['request'] = self.request
        d['model'] = self.model
        d['model_name'] = self.model.__name__
        return d

    def assign_from_form(self, item):
        #item.field = self.request.field
        raise NotImplementedError()

    def post_item(self):
        if 'id' in self.request.POST and self.request.POST['id']:
            item_id = self.request.POST['id']
            query = DBSession.query(self.model).filter_by(id=item_id)
            item = query.first() or self.model()
        else:
            item = self.model()
        self.assign_from_form(item)
        DBSession.add(item)
        DBSession.flush()

        self.request.session.flash(('info', 'Saved!'))
        route_name = 'admin_' + self.model.__name__.lower() + 's'
        location = self.request.route_url(route_name, _query={'id': item.id})
        DBSession.expire_all()
        return HTTPSeeOther(location=location)

    def get_item(self, id):
        item = DBSession.query(self.model).filter_by(id=id).first()
        template = 'admin/item.mako'
        if item is None:
            raise HTTPNotFound()
        return render_to_response(self.item_template or template,
                                  self.tvars(dict(item=item)))

    def list_items(self):
        items = DBSession.query(self.model).order_by(self.model.id).all()
        template = 'admin/list.mako'
        return render_to_response(self.list_template or template,
                                  self.tvars(dict(items=items)))

    def _get_uid(self, input):
        if input.startswith('#'):
            return input[1:]
        user = DBSession.query(User).filter_by(username=input).first()
        if not user:
            # TODO: handle that correctly
            raise HTTPBadRequest()
        return user.id

    def __call__(self):
        if self.request.method == 'POST':
            return self.post_item()
        else:
            if 'id' in self.request.GET:
                return self.get_item(self.request.GET['id'])
            else:
                return self.list_items()


@view_config(route_name='admin_users', permission='admin')
class AdminUsers(AdminView):
    model = User
    item_template = 'admin/item_user.mako'
    list_template = 'admin/list_user.mako'

    def assign_from_form(self, item):
        post = self.request.POST
        item.username = post['username']
        item.email = post['email'] or None
        if post['password']:
            item.set_password(post['password'])
        item.is_active = 'is_active' in post
        item.is_admin = 'is_admin' in post
        if post['paid_until']:
            item.paid_until = parser.parse(post['paid_until'])
        else:
            item.paid_until = None


@view_config(route_name='admin_orders', permission='admin')
class AdminOrders(AdminView):
    model = Order

    def assign_from_form(self, item):
        post = self.request.POST
        item.uid = self._get_uid(post['user'])
        item.start_date = post['start_date']
        item.close_date = post['close_date'] or None
        item.amount = post['amount']
        item.paid_amount = post['paid_amount']
        item.time = post['time']
        item.method = post['method']  # TODO: permit text values
        item.paid = 'paid' in post
        #item.payment = post['payment']


@view_config(route_name='admin_giftcodes', permission='admin')
class AdminGiftCodes(AdminView):
    model = GiftCode

    def assign_from_form(self, item):
        post = self.request.POST
        item.code = post['code']
        item.time = post['time']
        item.free_only = 'free_only' in post
        #item.used = self._get_uid(post['used'])


@view_config(route_name='admin_gateways', permission='admin')
class AdminGateways(AdminView):
    model = Gateway

    def assign_from_form(self, item):
        post = self.request.POST
        item.token = post['token'] or None
        item.name = post['name'] or None
        item.isp_name = post['isp_name'] or None
        item.isp_url = post['isp_url'] or None
        item.country = post['country'] or None
        item.ipv4 = post['ipv4'] or None
        item.ipv6 = post['ipv6'] or None
        item.bps = int(post['bps']) or None
        item.enabled = 'enabled' in post


