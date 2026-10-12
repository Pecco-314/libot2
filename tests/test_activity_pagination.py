import unittest
from unittest.mock import AsyncMock, patch

from src.spider import wrapper


def item(activity_id, timestamp, *, pinned=False):
    modules = {'module_author': {'pub_ts': timestamp, 'name': '三理Mit3uri', 'mid': 10}, 'module_tag': None}
    if pinned:
        modules['module_tag'] = {'text': '置顶'}
    return dict(id_str=str(activity_id), modules=modules, type='DYNAMIC_TYPE_DRAW')


def response(items, *, offset='', has_more=False):
    return {'body': {'data': {'items': items, 'offset': offset, 'has_more': has_more}}}


class ActivityPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_old_item_does_not_stop_pagination(self):
        api = AsyncMock(side_effect=[
            response([item(1, 10, pinned=True), item(3, 300)], offset='page2', has_more=True),
            response([item(2, 200), item(1, 10, pinned=True)], offset='page3', has_more=True),
        ])
        with patch.object(wrapper.api, 'get_space_history', api), patch.object(wrapper.asyncio, 'sleep', AsyncMock()):
            rows = await wrapper.get_space_history(10, since=200)
        self.assertEqual({row.activity_id for row in rows}, {'1', '2', '3'})
        self.assertEqual(api.await_count, 2)
        api.assert_any_await(10, 'page2')

    async def test_no_boundary_only_reads_first_page(self):
        api = AsyncMock(return_value=response([item(3, 300)], offset='page2', has_more=True))
        with patch.object(wrapper.api, 'get_space_history', api):
            await wrapper.get_space_history(10)
        api.assert_awaited_once_with(10, '')

    async def test_repeated_offset_raises_instead_of_advancing_boundary(self):
        api = AsyncMock(return_value=response([item(3, 300)], offset='page2', has_more=True))
        with patch.object(wrapper.api, 'get_space_history', api), patch.object(wrapper.asyncio, 'sleep', AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'offset'):
                await wrapper.get_space_history(10, since=200)

    async def test_page_limit_raises_instead_of_returning_partial_history(self):
        api = AsyncMock(return_value=response([item(3, 300)], offset='page2', has_more=True))
        with patch.object(wrapper.api, 'get_space_history', api):
            with self.assertRaisesRegex(RuntimeError, '历史边界'):
                await wrapper.get_space_history(10, since=200, max_pages=1)

    async def test_empty_end_is_safe(self):
        api = AsyncMock(return_value=response([]))
        with patch.object(wrapper.api, 'get_space_history', api):
            self.assertEqual(await wrapper.get_space_history(10, since=200), [])

    async def test_empty_page_with_more_data_raises(self):
        api = AsyncMock(return_value=response([], offset='page2', has_more=True))
        with patch.object(wrapper.api, 'get_space_history', api):
            with self.assertRaisesRegex(RuntimeError, '空页'):
                await wrapper.get_space_history(10, since=200)

    async def test_missing_author_does_not_create_fake_boundary(self):
        incomplete = {'id_str': '4', 'modules': {'module_author': None, 'module_tag': None}}
        api = AsyncMock(side_effect=[
            response([incomplete, item(3, 300)], offset='page2', has_more=True),
            response([item(2, 200)]),
        ])
        with patch.object(wrapper.api, 'get_space_history', api), patch.object(wrapper.asyncio, 'sleep', AsyncMock()):
            await wrapper.get_space_history(10, since=200)
        self.assertEqual(api.await_count, 2)

    async def test_api_error_reports_reason_instead_of_attribute_error(self):
        api = AsyncMock(return_value={'body': {'code': -352, 'message': 'risk control', 'data': None}})
        with patch.object(wrapper.api, 'get_space_history', api):
            with self.assertRaisesRegex(ValueError, 'code=-352'):
                await wrapper.get_space_history(10, since=200)
