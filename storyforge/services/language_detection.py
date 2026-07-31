from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Final


LANGUAGE_NAMES_ZH: Final[dict[str, str]] = {
    "en": "英语",
    "zh-Hans": "简体中文",
    "zh-Hant": "繁体中文",
    "es": "西班牙语",
    "pt": "葡萄牙语",
    "id": "印度尼西亚语",
    "fr": "法语",
    "de": "德语",
    "it": "意大利语",
    "hi": "印地语",
    "ja": "日语",
    "ko": "韩语",
    "mixed": "混合语种",
    "other": "其他语种",
    "unknown": "未识别",
}

LANGUAGE_ALIASES: Final[dict[str, str]] = {
    "en": "en",
    "en-us": "en",
    "en_us": "en",
    "english": "en",
    "英语": "en",
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh_cn": "zh-Hans",
    "zh-hans": "zh-Hans",
    "chinese": "zh-Hans",
    "simplified chinese": "zh-Hans",
    "简体中文": "zh-Hans",
    "简中": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh_tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-hant": "zh-Hant",
    "traditional chinese": "zh-Hant",
    "繁体中文": "zh-Hant",
    "繁中": "zh-Hant",
    "es": "es",
    "es-es": "es",
    "spanish": "es",
    "西班牙语": "es",
    "pt": "pt",
    "pt-br": "pt",
    "pt-pt": "pt",
    "portuguese": "pt",
    "葡萄牙语": "pt",
    "id": "id",
    "id-id": "id",
    "indonesian": "id",
    "bahasa indonesia": "id",
    "印度尼西亚语": "id",
    "印尼语": "id",
    "fr": "fr",
    "fr-fr": "fr",
    "french": "fr",
    "法语": "fr",
    "de": "de",
    "de-de": "de",
    "german": "de",
    "德语": "de",
    "it": "it",
    "it-it": "it",
    "it_it": "it",
    "ita": "it",
    "italian": "it",
    "italiano": "it",
    "意大利语": "it",
    "hi": "hi",
    "hi-in": "hi",
    "hi_in": "hi",
    "hin": "hi",
    "hindi": "hi",
    "हिन्दी": "hi",
    "हिंदी": "hi",
    "印地语": "hi",
    "ja": "ja",
    "ja-jp": "ja",
    "japanese": "ja",
    "日语": "ja",
    "ko": "ko",
    "ko-kr": "ko",
    "korean": "ko",
    "韩语": "ko",
    "mixed": "mixed",
    "混合语种": "mixed",
    "other": "other",
    "其他语种": "other",
    "unknown": "unknown",
    "未识别": "unknown",
}


@dataclass(frozen=True, slots=True)
class LanguageDetection:
    code: str
    display_name: str
    confidence: float

    def to_dict(self) -> dict[str, str | float]:
        return asdict(self)


_LATIN_WORD_RE = re.compile(r"[a-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f]+", re.I)

# Function words are much more stable across fiction genres than named entities.
# Keep the sets intentionally conservative: ambiguous one-letter words and words
# shared by neighbouring languages are omitted or given no special treatment.
_LATIN_MARKERS: Final[dict[str, frozenset[str]]] = {
    "en": frozenset(
        """
        the and that this these those was were is are been being have has had do
        does did with without from into about after before while when where why
        how who whom whose would could should might must will shall not never
        he she they them his her their ours yours myself himself herself
        something nothing everything because although however then there here
        said asked replied thought knew looked felt walked turned smiled
        """.split()
    ),
    "es": frozenset(
        """
        que como cuando donde porque para pero aunque mientras desde hasta sobre
        entre estaba estaban había habían tiene tienen había sido nunca siempre
        ella ellos ellas nosotros ustedes suyo suya quien nadie algo nada todo
        entonces después antes dijo preguntó respondió pensó sabía miró sintió
        volvió hizo hacía podía quería también todavía
        """.split()
    ),
    "pt": frozenset(
        """
        que como quando onde porque para porém embora enquanto desde até sobre
        entre estava estavam havia tinham tenho tem têm nunca sempre ela eles
        elas nós vocês seu sua ninguém algo nada tudo então depois antes disse
        perguntou respondeu pensou sabia olhou sentiu voltou fez fazia podia
        queria também ainda não uma
        """.split()
    ),
    "id": frozenset(
        """
        yang dan dengan untuk dari dalam pada karena tetapi namun ketika dimana
        mengapa bagaimana adalah telah sudah belum akan tidak bukan juga masih
        dia mereka kami kita kalian aku saya kamu dirinya siapa seseorang tidak
        apa semua kemudian setelah sebelum berkata bertanya menjawab berpikir
        tahu melihat merasa kembali membuat bisa ingin hanya sangat
        """.split()
    ),
    "fr": frozenset(
        """
        que qui quand où pourquoi comment pour mais pourtant bien que lorsque
        depuis jusque sur entre était étaient avait avaient être avoir jamais
        toujours elle elles nous vous leur personne quelque chose rien tout
        alors après avant dit demanda répondit pensa savait regarda sentit
        revint faisait pouvait voulait aussi encore dans avec sans
        """.split()
    ),
    "de": frozenset(
        """
        der die das den dem des und dass dieser diese dieses war waren ist sind
        gewesen hat hatte hatten nicht niemals immer aber obwohl während weil
        wenn wo warum wie wer wem dessen für von mit ohne zwischen über unter
        sie er ihnen ihr ihre wir euch niemand etwas nichts alles dann danach
        vorher sagte fragte antwortete dachte wusste sah fühlte ging konnte
        wollte würde zurück
        """.split()
    ),
    "it": frozenset(
        """
        che quando dove perché come per però mentre dalla fino sopra tra era
        erano aveva avevano essere avere mai sempre lei loro noi voi suo sua
        nessuno qualcosa niente tutto allora dopo prima disse chiese rispose
        pensò sapeva guardò sentì tornò fece faceva poteva voleva anche ancora
        nella della senza questo questa questi quelle
        """.split()
    ),
}

_DISTINCTIVE_MARKERS: Final[dict[str, frozenset[str]]] = {
    "en": frozenset("the would could should herself himself thought through".split()),
    "es": frozenset("estaba había habían preguntó respondió también todavía después".split()),
    "pt": frozenset("não porém vocês ninguém perguntou respondeu também então".split()),
    "id": frozenset("yang dengan tidak sudah belum mereka karena kemudian berkata".split()),
    "fr": frozenset("était avaient répondit quelque personne lorsque pourquoi".split()),
    "de": frozenset("nicht würde während niemand antwortete wusste zurück".split()),
    "it": frozenset(
        "perché però nessuno qualcosa niente allora disse chiese rispose sapeva guardò sentì tornò".split()
    ),
}

# Common script-specific variants. A manuscript does not need to contain every
# item; several occurrences are enough to choose a Chinese writing system.
_SIMPLIFIED_HINTS: Final[frozenset[str]] = frozenset(
    "万与专业东丝两严丧个临为丽举么义乌乐乔习乡书买乱争于云亚产亩亲亿仅从仓仪们价众优会伞伟传伤伦体余侠侣侦侧侨俩债倾儿党兰关兴养兽冈册写军农冲决况冻净凉减凑凤凭凯击凿刘则刚创删别剂剑剧劝办务动劲劳势勋匀华协单卖卢卤卫却厅历厉压厌县叁参双发变叙叶号叹听启吴员呛呜咏咙响喷嘱团园围国图圆圣场坏块坚坛坝坞坟坠垄垒垦垫垭墙壮声壳壶处备复够头夹夺奋奖奥妇妈姊姗姜娄娱婶孙学宁宝实宠审宪宫宽宾对寻导将尔尘尝层屉届属岁岂岖岗岛岭岳峡币帅师帐帘帮干并广庄庆床库应庙庞废开异弃张弥弯弹强归当录彻忆忧怀态怂总恋恒恳恶惊惧惨惩惯愤愿慑戏户扑执扩扫扬扰抚抛抢护报担拟拢拣拥拦拨择挂挚挛挝挞挟挠挡挣挥损捡换据掳掴掷掸掺揽搀搁搂搅摄摆摇撑撵撷撸携摄摆数斋斗断无旧时旷昙显晋晓晕暂术朴机杀杂权条来杨杰极构枪柜柠标栈栋栏树样桥梦检楼欢欧欲歼殴毁毕气汇汉汤沟没泪泽洁浓济浏浑浅浆测浇浊涂涛涝涞涡涣涤润涧涨涩淀渊渔渗温湾湿溃溅滚滞满滤滥滨滩潇潜澜濑灭灯灵灾灿炉炖炼烁烂烛烟烦烧烫热爱爷牵牺状犹狈独狭狮狱猎猪猫献玛环现琼电画疗疟疯痈盐监盖盘着睁瞒矿码砖础确碍礼祷祸离种积称稳穷窃竞笔笼签简箩篮类粮紧纠红纤约级纪纬纯纱纲纳纵纷纸纹纽线练组细织终绊绍经绑绒结绕绘给络绝统绣继绩绪续绳维绵综绿缀缉缘缚缝缩缴罢罗罚罴羁翘耸联聪肃肠肤肾胆胜胀胶脉脏脑脚脱脸腊腻腾舆舰舱艺节芜苇苏范茧荐荡荣荤药莲获莹萝营萨葱蒋蓝蔼蕴虫虾蚀虽蚂蛊蛮补袜袭装见观规觅视览觉触誉计订认讥讨让训议讯记讲讳讴许论设访证评识诈诉诊词译试诗诚话诞询该详语误说请诸诺读课谁调谈谊谋谎谢谣谤谨谱贝负贡财责贤败账货质贩贪贫贬购贮贯贴贵贷贸费贺贼贾赃资赋赌赏赐赔赖赚赛赞赠赵赶趋跃践跷跸车轧轨轩转轮软轰轴轻载较辅辆辈辉辑输辖辙边辽达迁过迈运还这进远违连迟适选逊递逻遗邮邻郑释里鉴钉钓钙钝钞钟钢钥钦钩钱钳钻铁铃铅铐铜铝铭银铺链销锁锅锈锋锐错锡锣锦键锯锻镁镇镜长门闩闪闭问闯闲间闷闹闻阀阁阅阔队阳阴阵阶际陆陈险随隐隶难雾霁静顶项顺须顽顾顿颁颂预领颇频题颜额风飘飞饥饭饮饰饱饲饼馆馋马驯驰驱驳驻驾骂骄骆骑骗骚骤鱼鲁鲜鸟鸡鸣鸭鸽鸿鹅鹤麦黄齐齿龙龟"
)
_TRADITIONAL_HINTS: Final[frozenset[str]] = frozenset(
    "萬與專業東絲兩嚴喪個臨為麗舉麼義烏樂喬習鄉書買亂爭於雲亞產畝親億僅從倉儀們價眾優會傘偉傳傷倫體餘俠侶偵側僑倆債傾兒黨蘭關興養獸岡冊寫軍農衝決況凍淨涼減湊鳳憑凱擊鑿劉則剛創刪別劑劍劇勸辦務動勁勞勢勳勻華協單賣盧鹵衛卻廳歷厲壓厭縣參雙發變敘葉號嘆聽啟吳員嗆嗚詠嚨響噴囑團園圍國圖圓聖場壞塊堅壇壩塢墳墜壟壘墾墊埡牆壯聲殼壺處備復夠頭夾奪奮獎奧婦媽姊姍薑婁娛嬸孫學寧寶實寵審憲宮寬賓對尋導將爾塵嘗層屜屆屬歲豈嶇崗島嶺嶽峽幣帥師帳簾幫乾並廣莊慶床庫應廟龐廢開異棄張彌彎彈強歸當錄徹憶憂懷態慫總戀恆懇惡驚懼慘懲慣憤願懾戲戶撲執擴掃揚擾撫拋搶護報擔擬攏揀擁攔撥擇掛摯攣撾撻挾撓擋掙揮損撿換據擄摑擲撣摻攬攙擱摟攪攝擺搖撐攆擷擼攜數齋鬥斷無舊時曠曇顯晉曉暈暫術樸機殺雜權條來楊傑極構槍櫃檸標棧棟欄樹樣橋夢檢樓歡歐欲殲毆毀畢氣匯漢湯溝沒淚澤潔濃濟瀏渾淺漿測澆濁塗濤澇淶渦渙滌潤澗漲澀澱淵漁滲溫灣濕潰濺滾滯滿濾濫濱灘瀟潛瀾瀨滅燈靈災燦爐燉煉爍爛燭煙煩燒燙熱愛爺牽犧狀猶狽獨狹獅獄獵豬貓獻瑪環現瓊電畫療瘧瘋癰鹽監蓋盤著睜瞞礦碼磚礎確礙禮禱禍離種積稱穩窮竊競筆籠簽簡籮籃類糧緊糾紅纖約級紀緯純紗綱納縱紛紙紋紐線練組細織終絆紹經綁絨結繞繪給絡絕統繡繼績緒續繩維綿綜綠綴緝緣縛縫縮繳罷羅罰羆羈翹聳聯聰肅腸膚腎膽勝脹膠脈髒腦腳脫臉臘膩騰輿艦艙藝節蕪葦蘇範繭薦蕩榮葷藥蓮獲瑩蘿營薩蔥蔣藍藹蘊蟲蝦蝕雖螞蠱蠻補襪襲裝見觀規覓視覽覺觸譽計訂認譏討讓訓議訊記講諱謳許論設訪證評識詐訴診詞譯試詩誠話誕詢該詳語誤說請諸諾讀課誰調談誼謀謊謝謠謗謹譜貝負貢財責賢敗賬貨質販貪貧貶購貯貫貼貴貸貿費賀賊賈贓資賦賭賞賜賠賴賺賽讚贈趙趕趨躍踐蹺蹕車軋軌軒轉輪軟轟軸輕載較輔輛輩輝輯輸轄轍邊遼達遷過邁運還這進遠違連遲適選遜遞邏遺郵鄰鄭釋裡鑒釘釣鈣鈍鈔鐘鋼鑰欽鉤錢鉗鑽鐵鈴鉛銬銅鋁銘銀鋪鏈銷鎖鍋鏽鋒銳錯錫鑼錦鍵鋸鍛鎂鎮鏡長門閂閃閉問闖閒間悶鬧聞閥閣閱闊隊陽陰陣階際陸陳險隨隱隸難霧霽靜頂項順須頑顧頓頒頌預領頗頻題顏額風飄飛飢飯飲飾飽飼餅館饞馬馴馳驅駁駐駕罵驕駱騎騙騷驟魚魯鮮鳥雞鳴鴨鴿鴻鵝鶴麥黃齊齒龍龜"
)


def normalize_language_code(value: object, *, allow_auto: bool = False) -> str:
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw or raw.casefold() == "auto":
        if allow_auto:
            return "auto"
        raise ValueError("language is required")
    code = LANGUAGE_ALIASES.get(raw.casefold())
    if code is None:
        supported = ", ".join(LANGUAGE_NAMES_ZH)
        raise ValueError(f"unsupported language {raw!r}; expected one of: {supported}")
    return code


def language_display_name(code: object) -> str:
    normalized = normalize_language_code(code)
    return LANGUAGE_NAMES_ZH[normalized]


def _sample_manuscript(text: str, maximum: int = 120_000) -> str:
    """Sample beginning, middle and ending so very long books stay inexpensive."""

    if len(text) <= maximum:
        return text
    width = maximum // 3
    middle = len(text) // 2
    return "\n".join(
        (text[:width], text[middle - width // 2 : middle + width // 2], text[-width:])
    )


def _script_counts(text: str) -> dict[str, int]:
    counts = {
        "latin": 0,
        "han": 0,
        "kana": 0,
        "hangul": 0,
        "devanagari": 0,
        "other": 0,
    }
    for character in text:
        codepoint = ord(character)
        if (
            0x0041 <= codepoint <= 0x005A
            or 0x0061 <= codepoint <= 0x007A
            or 0x00C0 <= codepoint <= 0x024F
        ):
            counts["latin"] += 1
        elif (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        ):
            counts["han"] += 1
        elif 0x3040 <= codepoint <= 0x30FF or 0x31F0 <= codepoint <= 0x31FF:
            counts["kana"] += 1
        elif (
            0x1100 <= codepoint <= 0x11FF
            or 0x3130 <= codepoint <= 0x318F
            or 0xAC00 <= codepoint <= 0xD7AF
        ):
            counts["hangul"] += 1
        elif (
            0x0900 <= codepoint <= 0x097F or 0xA8E0 <= codepoint <= 0xA8FF
        ) and (character.isalpha() or unicodedata.category(character).startswith("M")):
            counts["devanagari"] += 1
        elif character.isalpha():
            counts["other"] += 1
    return counts


def _result(code: str, confidence: float) -> LanguageDetection:
    return LanguageDetection(
        code=code,
        display_name=LANGUAGE_NAMES_ZH[code],
        confidence=round(max(0.0, min(1.0, confidence)), 3),
    )


def _detect_cjk(text: str, counts: dict[str, int]) -> LanguageDetection | None:
    han = counts["han"]
    kana = counts["kana"]
    hangul = counts["hangul"]
    if hangul >= 8 and hangul >= max(1, kana * 2):
        confidence = 0.82 + min(0.16, math.log10(hangul + 1) * 0.06)
        return _result("ko", confidence)
    if kana >= 8 or (kana >= 4 and kana / max(1, kana + han) >= 0.04):
        confidence = 0.82 + min(0.16, math.log10(kana + 1) * 0.07)
        return _result("ja", confidence)
    if han < 12:
        return None
    simplified = sum(character in _SIMPLIFIED_HINTS for character in text)
    traditional = sum(character in _TRADITIONAL_HINTS for character in text)
    if simplified and traditional and min(simplified, traditional) >= max(3, 0.28 * max(simplified, traditional)):
        return _result("mixed", 0.78)
    winner = max(simplified, traditional)
    if winner < 2:
        # Pure shared Han characters cannot safely reveal a writing system.
        return _result("unknown", 0.0)
    code = "zh-Hans" if simplified > traditional else "zh-Hant"
    margin = abs(simplified - traditional) / max(1, simplified + traditional)
    evidence = min(1.0, winner / 12)
    return _result(code, 0.66 + margin * 0.18 + evidence * 0.14)


def _detect_latin(text: str, counts: dict[str, int]) -> LanguageDetection:
    normalized = unicodedata.normalize("NFC", text).casefold()
    words = _LATIN_WORD_RE.findall(normalized)
    # A title, filename, code, or one short sentence is not enough evidence.
    if len(words) < 12 or counts["latin"] < 50:
        return _result("unknown", 0.0)

    scores: dict[str, float] = {}
    hits: dict[str, int] = {}
    for code, markers in _LATIN_MARKERS.items():
        score = 0.0
        hit_count = 0
        distinctive = _DISTINCTIVE_MARKERS[code]
        for word in words:
            if word not in markers:
                continue
            hit_count += 1
            score += 2.2 if word in distinctive else 1.0
        scores[code] = score
        hits[code] = hit_count

    # Orthography contributes supporting evidence but never decides alone.
    scores["es"] += 1.5 * sum(normalized.count(item) for item in ("¿", "¡"))
    scores["pt"] += 0.7 * sum(normalized.count(item) for item in ("ã", "õ"))
    scores["fr"] += 0.4 * sum(normalized.count(item) for item in ("œ", "ç"))
    scores["de"] += 0.7 * sum(normalized.count(item) for item in ("ä", "ö", "ü", "ß"))

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    (best_code, best_score), (_, second_score) = ranked[:2]
    if best_score < 5.0 or hits[best_code] < 4:
        return _result("unknown", 0.0)

    if second_score >= 5.0 and second_score / best_score >= 0.62:
        balance = second_score / best_score
        return _result("mixed", 0.68 + min(0.18, balance * 0.18))

    margin = (best_score - second_score) / max(1.0, best_score)
    density = min(1.0, best_score / max(10.0, len(words) * 0.22))
    confidence = 0.56 + margin * 0.25 + density * 0.16
    if confidence < 0.64:
        return _result("unknown", 0.0)
    return _result(best_code, confidence)


def detect_language(text: object) -> LanguageDetection:
    """Classify manuscript prose without network access or model dependencies.

    The detector favours ``unknown`` over a confident-looking mistake. It uses
    Unicode script evidence for CJK languages and conservative function-word
    evidence for seven Latin-script languages. Filenames and titles are never
    consulted by this function.
    """

    if not isinstance(text, str):
        return _result("unknown", 0.0)
    sampled = _sample_manuscript(unicodedata.normalize("NFC", text))
    counts = _script_counts(sampled)
    letters = sum(counts.values())
    if letters < 12:
        return _result("unknown", 0.0)

    significant_scripts = [
        name
        for name in ("latin", "han", "kana", "hangul", "devanagari", "other")
        if counts[name] >= 20 and counts[name] / letters >= 0.16
    ]
    devanagari = counts["devanagari"]
    if devanagari >= 12:
        mixed_with = any(
            counts[name] >= 20 and counts[name] / letters >= 0.16
            for name in ("latin", "han", "kana", "hangul", "other")
        )
        if mixed_with:
            return _result("mixed", 0.86)
        if devanagari / letters >= 0.70:
            confidence = 0.86 + min(0.12, math.log10(devanagari + 1) * 0.05)
            return _result("hi", confidence)
    asian_present = counts["han"] + counts["kana"] + counts["hangul"] >= 12
    if asian_present and counts["latin"] >= 30 and counts["latin"] / letters >= 0.16:
        return _result("mixed", 0.88)
    if counts["other"] >= 20 and counts["other"] / letters >= 0.50:
        return _result("other", 0.9)
    if len(significant_scripts) >= 2 and "other" in significant_scripts:
        return _result("mixed", 0.84)

    cjk = _detect_cjk(sampled, counts)
    if cjk is not None:
        return cjk
    if counts["latin"] >= 50:
        return _detect_latin(sampled, counts)
    return _result("unknown", 0.0)


__all__ = [
    "LANGUAGE_ALIASES",
    "LANGUAGE_NAMES_ZH",
    "LanguageDetection",
    "detect_language",
    "language_display_name",
    "normalize_language_code",
]
