from decimal import Decimal

import pytest
from sqlalchemy import select, func

from app.models import User, Product, CartItem, PriceTier, Brand, Category
from app.bot.routers import customer, common
from tests.fakes import FakeUser, FakeMessage, FakeCallback, make_state


async def seed(s, count=1, kind='retail'):
    user = User(telegram_id=7000, first_name='Buyer', phone='0912', is_active=True,
                is_approved=True, customer_type=kind)
    s.add(user)
    products = [Product(internal_sku=f'P-{n}', name=f'Product {n:02}', base_price=100,
                        wholesale_price=80, stock_quantity=10) for n in reversed(range(count))]
    s.add_all(products)
    await s.commit()
    return user, products


def callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_catalog_is_one_sorted_message_with_links_and_pagination(db):
    async with db() as s:
        _, products = await seed(s, 14)
        hidden = Product(name='Hidden', internal_sku='Hidden', base_price=10, is_active=False)
        s.add(hidden)
        await s.commit()
    message = FakeMessage(user=FakeUser(id=7000))
    await customer.products_cmd(message)
    assert len(message.answers) == 1
    text = message.answers[0]
    assert text.index('Product 00') < text.index('Product 01') < text.index('Product 05')
    assert 'Product 06' not in text and 'Hidden' not in text
    assert text.count('برای سفارش محصول روی لینک زیر کلیک کنید') == 6
    assert '?start=product_' in text
    assert 'catalog:all:0:1' in callbacks(message.markups[0])
    assert not any(c and c.startswith(('addcart:', 'showprod:')) for c in callbacks(message.markups[0]))
    assert set(callbacks(message.markups[0])) == {'catalog:all:0:1', 'admin:back'}
    cb = FakeCallback('catalog:all:0:1', user=FakeUser(id=7000), message=message)
    await customer.catalog_page(cb, make_state(7000))
    assert len(message.answers) == 1 and len(message.edits) == 1
    assert 'Product 06' in message.edits[-1] and 'Product 12' not in message.edits[-1]
    assert 'catalog:all:0:0' in callbacks(message.edit_markups[-1])
    assert 'catalog:all:0:2' in callbacks(message.edit_markups[-1])
    assert not any(c and c.startswith('showprod:') for c in callbacks(message.edit_markups[-1]))


@pytest.mark.asyncio
async def test_product_deep_link_shows_only_selected_product(db):
    async with db() as s:
        _, products = await seed(s, 3)
        product = products[1]
        pid, name = product.id, product.name
    message = FakeMessage(f'/start product_{pid}', user=FakeUser(id=7000))
    await common.start(message, make_state(7000))
    assert len(message.answers) == 1 and name in message.answers[0]
    assert f'addcart:{pid}' in callbacks(message.markups[0])
    assert 'فهرست محصولات' not in message.answers[0]


@pytest.mark.asyncio
async def test_product_button_only_shows_selected_product_and_rejects_inactive(db):
    async with db() as s:
        _, products = await seed(s, 2)
        pid = products[0].id
    cb = FakeCallback(f'showprod:{pid}', user=FakeUser(id=7000))
    await customer.show_product(cb)
    assert len(cb.message.answers) == 1
    assert f'addcart:{pid}' in callbacks(cb.message.markups[0])
    async with db() as s:
        (await s.get(Product, pid)).is_active = False
        await s.commit()
    cb2 = FakeCallback(f'showprod:{pid}', user=FakeUser(id=7000))
    await customer.show_product(cb2)
    assert not cb2.message.answers and cb2.alerts


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,base', [('retail', 100), ('wholesale', 80)])
async def test_plus_minus_reprices_tiers_in_place_and_add_is_idempotent(db, kind, base):
    async with db() as s:
        _, products = await seed(s, kind=kind)
        pid = products[0].id
        s.add(PriceTier(product_id=pid, min_qty=3, max_qty=None, unit_price=60))
        await s.commit()
    cb = FakeCallback(f'addcart:{pid}', user=FakeUser(id=7000))
    state = make_state(7000)
    await customer.ask_qty(cb, state)
    await customer.ask_qty(cb, state)  # Double tap must not add a second line or change quantity.
    async with db() as s:
        assert await s.scalar(select(func.count(CartItem.id))) == 1
        item = await s.scalar(select(CartItem))
        iid = item.id
        assert item.quantity == 1
    message = FakeMessage(user=FakeUser(id=42, is_bot=True))
    for action in ['cartinc', 'cartinc']:
        await customer.cart_change(FakeCallback(f'{action}:{iid}', user=FakeUser(id=7000), message=message))
    assert len(message.edits) == 2 and not message.answers
    assert '۱۸۰' in message.edits[-1]  # qty 3 × tier 60
    await customer.cart_change(FakeCallback(f'cartdec:{iid}', user=FakeUser(id=7000), message=message))
    from app.utils import fa_num
    assert fa_num(base * 2) in message.edits[-1]
    async with db() as s:
        assert (await s.get(CartItem, iid)).quantity == 2


@pytest.mark.asyncio
async def test_quantity_bounds_ownership_and_deleted_item(db):
    async with db() as s:
        _, products = await seed(s)
        pid = products[0].id
        products[0].stock_quantity = 1
        s.add(User(telegram_id=7001, phone='0912', is_active=True))
        await s.commit()
    await customer.ask_qty(FakeCallback(f'addcart:{pid}', user=FakeUser(id=7000)), make_state(7000))
    async with db() as s:
        iid = (await s.scalar(select(CartItem))).id
    for action, uid in [('cartinc', 7000), ('cartdec', 7000), ('cartinc', 7001), ('cartdel', 7001)]:
        cb = FakeCallback(f'{action}:{iid}', user=FakeUser(id=uid))
        await customer.cart_change(cb)
        assert cb.alerts
        async with db() as s:
            assert (await s.get(CartItem, iid)).quantity == 1
    cb = FakeCallback(f'cartdel:{iid}', user=FakeUser(id=7000))
    await customer.cart_change(cb)
    assert cb.message.edits
    async with db() as s:
        assert await s.get(CartItem, iid) is None
    stale = FakeCallback(f'cartinc:{iid}', user=FakeUser(id=7000))
    await customer.cart_change(stale)
    assert stale.alerts


@pytest.mark.asyncio
async def test_search_and_brand_pagination_preserve_filters(db):
    async with db() as s:
        _, products = await seed(s, 9)
        brand = Brand(name='Brand')
        s.add(brand)
        await s.flush()
        for p in products: p.brand_id = brand.id
        s.add(Product(name='Other', internal_sku='Other', base_price=999))
        await s.commit()
        bid = brand.id
    state = make_state(7000)
    await state.clear()
    message = FakeMessage('Product', user=FakeUser(id=7000))
    await customer.search_result(message, state)
    next_page = next(c for c in callbacks(message.markups[-1]) if c and c.startswith('catalog:search:'))
    await customer.catalog_page(FakeCallback(next_page, user=FakeUser(id=7000), message=message), state)
    assert 'Product 06' in message.edits[-1] and 'Other' not in message.edits[-1]
    cb = FakeCallback(f'catalog:brand:{bid}:1', user=FakeUser(id=7000))
    await customer.catalog_page(cb, state)
    assert 'Product 06' in cb.message.edits[-1] and 'Other' not in cb.message.edits[-1]
    await state.clear()
    expired = FakeCallback(next_page, user=FakeUser(id=7000))
    await customer.catalog_page(expired, state)
    assert expired.alerts and not expired.message.edits


@pytest.mark.asyncio
async def test_incomplete_and_pending_accounts_cannot_browse_or_add(db):
    async with db() as s:
        user, products = await seed(s)
        user.customer_type, user.is_approved = 'wholesale', False
        await s.commit()
        pid = products[0].id
    cb = FakeCallback(f'addcart:{pid}', user=FakeUser(id=7000))
    await customer.ask_qty(cb, make_state(7000))
    await customer.products_cmd(cb.message)
    async with db() as s:
        assert await s.scalar(select(func.count(CartItem.id))) == 0
    assert any('انتظار تأیید' in t for t in cb.message.answers)


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', ['product_abc', 'product_0', 'product_99999999999999999999999999'])
async def test_invalid_product_deep_links_are_handled(db, payload):
    async with db() as s:
        await seed(s)
    message = FakeMessage(f'/start {payload}', user=FakeUser(id=7000))
    await common.start(message, make_state(7000))
    assert any('نامعتبر' in text for text in message.answers)


@pytest.mark.asyncio
async def test_add_unavailable_product_is_rejected(db):
    async with db() as s:
        _, products = await seed(s)
        products[0].stock_quantity = 0
        await s.commit()
        pid = products[0].id
    cb = FakeCallback(f'addcart:{pid}', user=FakeUser(id=7000))
    await customer.ask_qty(cb, make_state(7000))
    assert cb.alerts
    async with db() as s:
        assert await s.scalar(select(func.count(CartItem.id))) == 0
