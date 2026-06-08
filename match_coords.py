#!/usr/bin/env python3
"""
将村落数据与区县级坐标匹配，生成带经纬度的完整数据集
使用ok_geo.csv中的区县级(deep=2)坐标数据
"""
import csv
import json
import re
import sys
from collections import defaultdict

# 增大CSV字段大小限制（polygon字段很大）
csv.field_size_limit(sys.maxsize)

def load_geo_data(filepath, max_rows=None):
    """加载ok_geo.csv，提取deep=2(区县级)的坐标数据"""
    counties = {}  # key: (province_name, city_name, county_name) -> (lon, lat)
    cities = {}    # key: (province_name, city_name) -> (lon, lat)
    provinces = {} # key: province_name -> (lon, lat)

    # 也建立简化索引: county_name -> [(lon, lat, full_info)]
    county_simple_index = defaultdict(list)

    # 省份名到自身的映射(用于直辖市)
    # 直辖市: 北京,天津,上海,重庆
    municipalities = {'北京市', '天津市', '上海市', '重庆市'}

    with open(filepath, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        count = 0
        for row in reader:
            if max_rows and count >= max_rows:
                break
            count += 1

            deep = int(row['deep'])
            name = row['name'].strip()
            ext_path = row.get('ext_path', '').strip()
            geo = row.get('geo', '').strip()

            if not geo:
                continue

            # 解析坐标
            parts = geo.split()
            if len(parts) < 2:
                continue
            try:
                lon, lat = float(parts[0]), float(parts[1])
            except ValueError:
                continue

            # 解析ext_path: "北京市/北京市/东城区"
            path_parts = ext_path.split('/') if ext_path else [name]

            if deep == 0:  # 省份
                provinces[name] = (lon, lat)
            elif deep == 1:  # 城市
                prov = path_parts[0] if len(path_parts) > 0 else ''
                cities[(prov, name)] = (lon, lat)
            elif deep == 2:  # 区县
                prov = path_parts[0] if len(path_parts) > 0 else ''
                city = path_parts[1] if len(path_parts) > 1 else ''
                key = (prov, city, name)
                counties[key] = (lon, lat)
                county_simple_index[name].append((lon, lat, prov, city))

    print(f"加载完成: {len(provinces)} 省, {len(cities)} 市, {len(counties)} 区县")
    return counties, cities, provinces, county_simple_index


def extract_county_name(village):
    """从村落地址中提取区县名称"""
    district = village.get('district', '')
    city = village.get('city', '')
    province = village.get('province', '')

    if district:
        return district, city, province

    # 如果没有区县，尝试从城市名推断
    if city:
        return city, city, province

    return None, None, province


def match_village_to_coord(village, counties, county_simple_index, cities, provinces):
    """匹配村落到坐标"""
    province = village['province']
    city = village.get('city', '')
    district = village.get('district', '')

    # 策略1: 精确匹配市区县
    if district:
        # 尝试多种组合
        for prov_key in [province, province.replace('省','').replace('市',''), '']:
            for city_key in [city, city.replace('市','').replace('地区','').replace('自治州','').replace('盟',''), '']:
                if not prov_key and not city_key:
                    continue
                # 构建可能的key
                candidates = [
                    (prov_key, city_key, district),
                ]
                if prov_key:
                    candidates.append((prov_key, '', district))
                if city_key:
                    candidates.append(('', city_key, district))

                for key in candidates:
                    if key in counties:
                        return counties[key]

    # 策略2: 模糊匹配区县名
    if district:
        # 尝试部分名称匹配
        district_short = district.replace('区','').replace('县','').replace('市','').replace('自治县','')

        # 在county_simple_index中搜索
        for name, entries in county_simple_index.items():
            name_short = name.replace('区','').replace('县','').replace('市','').replace('自治县','')
            if district_short == name_short or district in name or name in district:
                # 检查省份匹配
                for lon, lat, entry_prov, entry_city in entries:
                    if province_match(province, entry_prov):
                        return (lon, lat)
                # 如果没有省份匹配，返回第一个
                return (entries[0][0], entries[0][1])

    # 策略3: 使用城市坐标
    if city:
        for (prov_key, city_key), (lon, lat) in cities.items():
            if city_key == city or city in city_key or city_key in city:
                if province_match(province, prov_key):
                    return (lon, lat)

    # 策略4: 使用省份坐标
    for prov_name, (lon, lat) in provinces.items():
        if province_match(province, prov_name):
            return (lon, lat)

    return None


def province_match(p1, p2):
    """检查两个省份名是否匹配"""
    if not p1 or not p2:
        return True
    p1 = p1.replace('省','').replace('市','').replace('自治区','').replace('壮族','').replace('回族','').replace('维吾尔','').replace('特别行政区','')
    p2 = p2.replace('省','').replace('市','').replace('自治区','').replace('壮族','').replace('回族','').replace('维吾尔','').replace('特别行政区','')
    return p1 == p2 or p1 in p2 or p2 in p1


def add_random_jitter(lon, lat, key, scale=0.01):
    """添加微小随机偏移，避免同一区县村落完全重叠
    key: 唯一标识(如村落全地址)，确保每个村落的偏移不同
    """
    import random
    random.seed(hash(key) % 1000000)
    jitter_lon = (random.random() - 0.5) * scale
    jitter_lat = (random.random() - 0.5) * scale
    return lon + jitter_lon, lat + jitter_lat


def main():
    print("加载geo数据...")
    counties, cities, provinces, county_simple_index = load_geo_data('ok_geo.csv')

    print("\n加载村落数据...")
    with open('villages_data.json', 'r', encoding='utf-8') as f:
        data = json.load(f)

    villages = data['villages']
    print(f"共 {len(villages)} 个村落待匹配")

    # 匹配
    matched = 0
    unmatched = 0
    unmatched_samples = []

    for i, v in enumerate(villages):
        coord = match_village_to_coord(v, counties, county_simple_index, cities, provinces)

        if coord:
            lon, lat = coord
            # 对同一区县的村落添加微小偏移
            lon, lat = add_random_jitter(lon, lat, v['full_address'], 0.005)
            v['longitude'] = round(lon, 6)
            v['latitude'] = round(lat, 6)
            matched += 1
        else:
            v['longitude'] = None
            v['latitude'] = None
            unmatched += 1
            if len(unmatched_samples) < 20:
                unmatched_samples.append(v)

        if (i + 1) % 1000 == 0:
            print(f"  已处理 {i+1}/{len(villages)}...")

    print(f"\n匹配结果: {matched} 成功, {unmatched} 失败")
    print(f"匹配率: {matched/len(villages)*100:.1f}%")

    if unmatched_samples:
        print("\n未匹配示例:")
        for v in unmatched_samples[:10]:
            print(f"  {v['full_address']} (省: {v['province']}, 市: {v.get('city','')}, 区县: {v.get('district','')})")

    # 保存结果
    output = {
        'villages': villages,
        'total': len(villages),
        'matched': matched,
        'unmatched': unmatched
    }

    with open('villages_with_coords.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n数据已保存至 villages_with_coords.json")

    # 也生成用于HTML的紧凑格式
    # 按批次和省份组织
    html_data = []
    for v in villages:
        if v['longitude'] is not None:
            html_data.append({
                'n': v.get('village', v['full_address']),  # 村名
                'f': v['full_address'],                      # 全地址
                'p': v['province'],                          # 省份
                'c': v.get('city', ''),                      # 城市
                'd': v.get('district', ''),                  # 区县
                't': v.get('town', ''),                      # 乡镇
                'b': int(v.get('batch_num', 1)),             # 批次
                'lng': v['longitude'],                       # 经度
                'lat': v['latitude']                         # 纬度
            })

    # 写入紧凑JS文件
    with open('villages_compact.json', 'w', encoding='utf-8') as f:
        json.dump(html_data, f, ensure_ascii=False, separators=(',', ':'))

    print(f"紧凑格式已保存至 villages_compact.json ({len(html_data)} 条)")

    # 按省份统计
    province_counts = defaultdict(int)
    for v in html_data:
        province_counts[v['p']] += 1

    print("\n各省村落统计:")
    for prov, cnt in sorted(province_counts.items(), key=lambda x: -x[1]):
        print(f"  {prov}: {cnt}")


if __name__ == '__main__':
    main()
