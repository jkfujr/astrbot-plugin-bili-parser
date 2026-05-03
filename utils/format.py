"""
格式化工具函数与 BV/AV 转换器
"""

from typing import Union

from astrbot.api import logger


class BvAvConverter:
    """BV号与AV号互相转换"""

    TABLE = 'FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf'
    TR = {c: i for i, c in enumerate(TABLE)}
    MAX_AVID = 1 << 51
    BASE = 58
    BVID_LEN = 12
    XOR = 23442827791579
    MASK = 2251799813685247

    @classmethod
    def bv_to_av(cls, bvid: str) -> str:
        """
        BV号转AV号
        :param bvid: BV号 (如: BV17x411w7KC)
        :return: AV号 (如: av170001)
        """
        if not bvid or not isinstance(bvid, str):
            raise ValueError('Invalid BV ID')

        # 移除可能的前缀并验证格式
        clean_bvid = bvid
        if clean_bvid.lower().startswith('bv'):
            clean_bvid = clean_bvid[2:]

        if len(clean_bvid) != 10:
            raise ValueError('Invalid BV ID format')

        chars = list('BV' + clean_bvid)

        # 交换字符位置
        chars[3], chars[9] = chars[9], chars[3]
        chars[4], chars[7] = chars[7], chars[4]

        # 计算av号
        temp = 0
        for char in chars[3:]:
            if char not in cls.TR:
                raise ValueError('Invalid character in BV ID')
            temp = temp * cls.BASE + cls.TR[char]

        avid = (temp & cls.MASK) ^ cls.XOR
        return f"av{avid}"

    @classmethod
    def av_to_bv(cls, avid: str) -> str:
        """
        AV号转BV号
        :param avid: AV号 (如: av170001)
        :return: BV号 (如: BV17x411w7KC)
        """
        if isinstance(avid, str):
            clean_avid = avid.lower().replace('av', '')
        else:
            clean_avid = str(avid)

        try:
            avid_int = int(clean_avid)
        except ValueError:
            raise ValueError('Invalid AV ID')

        if avid_int <= 0 or avid_int >= cls.MAX_AVID:
            raise ValueError('AV ID out of range')

        result = list('BV1' + ' ' * 9)
        temp = (cls.MAX_AVID | avid_int) ^ cls.XOR

        idx = cls.BVID_LEN - 1
        while temp > 0:
            result[idx] = cls.TABLE[temp % cls.BASE]
            temp //= cls.BASE
            idx -= 1

        # 交换字符位置
        result[3], result[9] = result[9], result[3]
        result[4], result[7] = result[7], result[4]

        return ''.join(result)


def normalize_video_id(video_id: str) -> str:
    """
    标准化视频ID为AV号格式，用于去重比较
    :param video_id: 视频ID (BV号或AV号)
    :return: 标准化的AV号
    """
    if not video_id or not isinstance(video_id, str):
        return video_id

    try:
        if video_id.lower().startswith('bv'):
            return BvAvConverter.bv_to_av(video_id)

        if video_id.lower().startswith('av'):
            return video_id.lower()

        if video_id.isdigit():
            return f"av{video_id}"

        return video_id
    except Exception as e:
        logger.warning(f"normalize_video_id error for {video_id}: {e}")
        return video_id


def format_number(value: Union[int, float, str]) -> str:
    """将大数字格式化为易读的文本，如 1万, 1.5亿"""
    if not isinstance(value, (int, float)):
        return str(value)
    if value >= 100000000:
        return f"{value/100000000:.1f}亿"
    if value >= 10000:
        return f"{value/10000:.1f}万"
    return str(value)


def format_live_status(status_code: int) -> str:
    """格式化直播间状态"""
    status_map = {
        0: "未开播",
        1: "直播中",
        2: "轮播中"
    }
    return status_map.get(status_code, "未知状态")
