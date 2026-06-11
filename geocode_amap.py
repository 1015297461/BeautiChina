#!/usr/bin/env python3
"""
使用高德地图API对村落进行精确地理编码
支持断点续传、自动限速、失败重试
"""
import requests
import json
import time
import os
from collections import Counter

# ========== 配置 ==========
API_KEY_FILE = 'api.md'
INPUT_FILE = 'villages_with_coords.json'
OUTPUT_FILE = 'villages_compact.json'
JS_OUTPUT_FILE = 'villages_data.js'
PROGRESS_FILE = 'geocode_progress.json'  # 断点续传文件

# 目标省份(仅处理这些)
# 第1批(西南)，共5省/市/区，2177个村落
TARGET_PROVINCES = ['云南省', '贵州省', '四川省', '重庆市', '西藏自治区']

# API限制
RATE_LIMIT_QPS = 25       # 高德QPS限制
RETRY_TIMES = 3            # 失败重试次数
RETRY_DELAY = 2            # 重试间隔(秒)
SAVE_INTERVAL = 50         # 每处理N个保存一次进度

def load_api_key():
    with open(API_KEY_FILE, 'r') as f:
        return f.read().strip()


def load_villages():
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data['villages']


def load_progress():
    """加载上次中断的进度"""
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_progress(progress):
    with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def build_address(v):
    """构造完整查询地址"""
    parts = [v['province']]
    if v.get('city'): parts.append(v['city'])
    if v.get('district'): parts.append(v['district'])
    if v.get('town'): parts.append(v['town'])
    if v.get('village'): parts.append(v['village'])
    addr = ''.join(parts)

    # 城市名(用于限定搜索范围)
    city_name = v.get('city', '') or v['province']
    # 海南省下辖区县直属省管，"海南"作为city参数会与青海省"海南藏族自治州"冲突
    # (ENGINE_RESPONSE_DATA_ERROR)，需保留"省"字
    if v['province'] == '海南省' and not v.get('city'):
        city_name = '海南省'
    else:
        city_name = city_name.replace('市', '').replace('省', '').replace('自治区', '')
    # 自治区等特殊处理
    if '自治州' in city_name:
        city_name = city_name.replace('自治州', '')
    if len(city_name) > 8:
        city_name = city_name[:6]

    return addr, city_name


def geocode(api_key, address, city):
    """调用高德地理编码API"""
    url = 'https://restapi.amap.com/v3/geocode/geo'
    params = {
        'key': api_key,
        'address': address,
        'city': city,
        'output': 'JSON'
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()

        if data['status'] == '1' and int(data['count']) > 0:
            loc = data['geocodes'][0]['location']
            lon, lat = loc.split(',')
            return {
                'success': True,
                'longitude': float(lon),
                'latitude': float(lat),
                'level': data['geocodes'][0].get('level', 'unknown'),
                'formatted_address': data['geocodes'][0].get('formatted_address', '')
            }
        else:
            return {
                'success': False,
                'reason': data.get('info', 'no result'),
                'count': int(data.get('count', 0))
            }

    except requests.exceptions.Timeout:
        return {'success': False, 'reason': 'timeout'}
    except requests.exceptions.ConnectionError:
        return {'success': False, 'reason': 'connection_error'}
    except Exception as e:
        return {'success': False, 'reason': str(e)}


def geocode_with_retry(api_key, address, city):
    """带重试的编码"""
    for attempt in range(RETRY_TIMES):
        result = geocode(api_key, address, city)
        if result['success']:
            return result
        if attempt < RETRY_TIMES - 1:
            time.sleep(RETRY_DELAY)
    return result  # 最后一次失败也返回


def main():
    api_key = load_api_key()
    villages = load_villages()
    progress = load_progress()

    # 筛选目标省份
    filtered = [v for v in villages if v['province'] in TARGET_PROVINCES]

    # 重新计算索引(使用full_address作为唯一key)
    # 统计哪些已经处理过
    done_keys = set(progress.get('done', []))
    todo = [(i, v) for i, v in enumerate(filtered)
            if v['full_address'] not in done_keys]

    total = len(filtered)
    done_count = len(done_keys)
    todo_count = len(todo)

    print(f'目标省份: {", ".join(TARGET_PROVINCES)}')
    print(f'村落总数: {total}')
    print(f'已完成:   {done_count}')
    print(f'待处理:   {todo_count}')
    print(f'API Key:  {api_key[:8]}...{api_key[-4:]}')
    print()

    if todo_count == 0:
        print('✅ 全部已完成，无需重新处理')
        return

    # 建立 villages 列表的索引映射(用于更新原始数据)
    village_index = {}
    for i, v in enumerate(villages):
        village_index[v['full_address']] = i

    success_count = progress.get('success_count', 0)
    fail_count = progress.get('fail_count', 0)
    results = progress.get('results', {})

    start_time = time.time()
    batch_start = time.time()

    for idx, (orig_idx, v) in enumerate(todo):
        addr, city = build_address(v)

        # 速率控制 (每10个检查一次)
        if (idx + 1) % 10 == 0:
            elapsed = time.time() - batch_start
            min_elapsed = 10 / RATE_LIMIT_QPS  # 10个请求至少需要的秒数
            if elapsed < min_elapsed:
                time.sleep(min_elapsed - elapsed)
            batch_start = time.time()

        # 调用API
        result = geocode_with_retry(api_key, addr, city)

        # 更新原始数据
        real_idx = village_index[v['full_address']]
        old_lon = villages[real_idx]['longitude']
        old_lat = villages[real_idx]['latitude']

        if result['success']:
            villages[real_idx]['longitude'] = result['longitude']
            villages[real_idx]['latitude'] = result['latitude']
            villages[real_idx]['geocode_level'] = result['level']
            villages[real_idx]['geocode_source'] = 'amap_api'
            results[v['full_address']] = 'success'
            success_count += 1

            # 打印进度(含坐标偏移量)
            dist = ((result['longitude'] - old_lon)**2 + (result['latitude'] - old_lat)**2)**0.5 * 111000
            print(f'  [{idx+1}/{todo_count}] ✅ {v["full_address"][:30]:30s} '
                  f'偏移{dist:.0f}m (旧→新)')
        else:
            villages[real_idx]['geocode_source'] = 'county_fallback'  # API失败，保留旧区县坐标
            villages[real_idx]['geocode_fail_reason'] = result.get('reason', 'unknown')
            results[v['full_address']] = f'failed: {result.get("reason", "unknown")}'
            fail_count += 1
            print(f'  [{idx+1}/{todo_count}] ❌ {v["full_address"][:30]:30s} '
                  f'{result.get("reason", "unknown")[:30]}')

        done_keys.add(v['full_address'])

        # 定期保存
        if (idx + 1) % SAVE_INTERVAL == 0:
            progress = {
                'done': list(done_keys),
                'success_count': success_count,
                'fail_count': fail_count,
                'results': results,
                'last_update': time.strftime('%Y-%m-%d %H:%M:%S')
            }
            save_progress(progress)

            # 同时保存数据更新
            save_updated_data(villages)
            elapsed = time.time() - start_time
            rate = (idx + 1) / elapsed * 60
            eta = (todo_count - idx - 1) / rate
            print(f'  💾 已保存 | 速率:{rate:.0f}个/分 | 预计剩余:{eta:.0f}分')
            print()

    # 最终保存
    progress = {
        'done': list(done_keys),
        'success_count': success_count,
        'fail_count': fail_count,
        'results': results,
        'last_update': time.strftime('%Y-%m-%d %H:%M:%S')
    }
    save_progress(progress)
    save_updated_data(villages)
    save_compact_and_js(villages)

    # 最终统计
    elapsed = time.time() - start_time
    print()
    print('=' * 50)
    print(f'✅ 处理完成!')
    print(f'   耗时:       {elapsed/60:.1f} 分钟')
    print(f'   成功:       {success_count}')
    print(f'   失败:       {fail_count}')
    print(f'   成功率:     {success_count/(success_count+fail_count)*100:.1f}%')
    print(f'   平均速率:   {todo_count/elapsed*60:.0f} 个/分')


def save_updated_data(villages):
    """保存更新后的完整数据"""
    output = {
        'villages': villages,
        'total': len(villages)
    }
    with open(INPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


def save_compact_and_js(villages):
    """生成紧凑JSON和JS文件"""
    compact = []
    for v in villages:
        compact.append({
            'n': v.get('village', v['full_address']),
            'f': v['full_address'],
            'p': v['province'],
            'c': v.get('city', ''),
            'd': v.get('district', ''),
            't': v.get('town', ''),
            'b': int(v.get('batch_num', 1)),
            'lng': v['longitude'],
            'lat': v['latitude'],
            's': v.get('geocode_source', 'county_center')  # 坐标来源: amap_api / county_center
        })

    # 紧凑JSON
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(compact, f, ensure_ascii=False, separators=(',', ':'))

    # JS文件
    with open(JS_OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(f'// 中国传统村落数据 - {len(compact)} 条记录\n')
        f.write('// 自动生成，请勿手动编辑\n')
        f.write('var VILLAGES_DATA = ')
        json.dump(compact, f, ensure_ascii=False, separators=(',', ':'))
        f.write(';\n')

    # 按来源统计
    sources = Counter(v.get('geocode_source', 'county_fallback') for v in villages)
    api_count = sources.get('amap_api', 0)
    fallback_count = sources.get('county_fallback', 0)
    total = api_count + fallback_count
    print(f'   高德API精确定位: {api_count}/{total} ({api_count/total*100:.1f}%)')
    print(f'   区县中心降级:    {fallback_count}/{total} ({fallback_count/total*100:.1f}%)')


if __name__ == '__main__':
    main()
