"""交易所（market 表）映射配置。

约定：所有映射的 key 统一用 `market_id`（英文短名，如 "beijing"），

"""

MARKET_ID = [
    "beifang",
    "beijing",
    "fujian",
    "guangdong",
    "guiyang",
    "hainan",
    "hangzhou",
    "shanghai",
    "zhengzhou",
]

MARKET_NAME = {
    "beifang": "北方大数据交易平台",
    "beijing": "北京大数据交易所",
    "fujian": "福建大数据交易平台",
    "guangdong": "广东数据交易所",
    "guiyang": "贵阳大数据交易所",
    "hainan": "海南省数据产品超市",
    "hangzhou": "杭州数据交易所",
    "shanghai": "上海数据交易所",
    "zhengzhou": "郑州数据交易中心",
}

MARKET_SHORTNAME = {
    "beifang": "北方",
    "beijing": "北京",
    "fujian": "福建",
    "guangdong": "广东",
    "guiyang": "贵阳",
    "hainan": "海南",
    "hangzhou": "杭州",
    "shanghai": "上海",
    "zhengzhou": "郑州",
}

# market 表字段 → 取值来源（按 market_id 索引的 dict）。
# 生成插入数据时按列取 MARKET_TABLE_COLUMNS_VALUE[col][market_id] 即可。
MARKET_TABLE_COLUMNS_VALUE = {
    "market_id": MARKET_ID,
    "name": MARKET_NAME,
    "short_name": MARKET_SHORTNAME,
}
