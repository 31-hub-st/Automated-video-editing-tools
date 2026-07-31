from __future__ import annotations

import unittest

from storyforge.services.language_detection import (
    detect_language,
    normalize_language_code,
)


class LanguageDetectionTests(unittest.TestCase):
    def test_detects_supported_story_languages(self) -> None:
        samples = {
            "en": "Last night, when I was getting ready to sleep, the phone rang. She said that my husband was with her, but I thought it was a prank. Then she asked why I never knew the truth about him.",
            "es": "Anoche, cuando estaba a punto de dormir, sonó el teléfono. Ella dijo que mi marido estaba con ella, pero pensé que era una broma. Entonces preguntó por qué yo nunca había sabido la verdad.",
            "pt": "Naquela noite, quando eu estava prestes a dormir, o telefone tocou. Ela disse que meu marido estava com ela, mas pensei que era uma brincadeira. Então perguntou por que eu nunca soube da verdade.",
            "id": "Tadi malam, ketika saya hendak tidur, telepon berdering. Dia berkata bahwa suami saya bersamanya, tetapi saya pikir itu hanya lelucon. Kemudian dia bertanya mengapa saya tidak tahu kebenarannya.",
            "fr": "Hier soir, lorsque j’étais sur le point de dormir, le téléphone a sonné. Elle a dit que mon mari était avec elle, mais je pensais que c’était une plaisanterie. Puis elle demanda pourquoi je ne savais rien.",
            "de": "Letzte Nacht, als ich schlafen gehen wollte, klingelte das Telefon. Sie sagte, dass mein Mann bei ihr war, aber ich dachte, es wäre ein Scherz. Dann fragte sie, warum ich die Wahrheit nicht wusste.",
            "it": "Ieri sera, quando stavo per dormire, squillò il telefono. Lei disse che mio marito era con lei, però pensavo fosse uno scherzo. Allora chiese perché non avevo mai saputo niente e rispose che ormai era troppo tardi.",
            "hi": "कल रात दस बजे, जब मैं सोने की तैयारी कर रही थी, फोन बजा। महिला ने कहा कि मेरा पति उसके साथ था, लेकिन मैंने सोचा कि यह मज़ाक है। फिर उसने पूछा कि मुझे सच्चाई कभी क्यों नहीं पता चली।",
            "zh-Hans": "昨晚十点，当我正准备睡觉时，电话突然响了。那个女人笑着说，我的丈夫就在她身边。我以为这只是一个恶作剧，可她接下来的话让我彻底愣住。",
            "zh-Hant": "昨晚十點，當我正準備睡覺時，電話突然響了。那個女人笑著說，我的丈夫就在她身邊。我以為這只是一個惡作劇，可她接下來的話讓我徹底愣住。",
            "ja": "昨夜十時、眠ろうとしていたときに電話が鳴りました。女性は私の夫がそばにいると言いました。冗談だと思いましたが、その次の言葉に私は凍りつきました。",
            "ko": "어젯밤 열 시, 잠자리에 들려고 할 때 전화가 울렸습니다. 그 여자는 내 남편이 자기 옆에 있다고 말했습니다. 농담인 줄 알았지만 다음 말에 나는 얼어붙었습니다.",
        }

        for expected, manuscript in samples.items():
            with self.subTest(language=expected):
                result = detect_language(manuscript)
                self.assertEqual(result.code, expected)
                self.assertGreaterEqual(result.confidence, 0.64)

    def test_short_codes_and_numbers_stay_unknown(self) -> None:
        for value in (
            "B73165",
            "Chapter 1",
            "1234567890",
            "A short title",
            "Roma bella amore notte famiglia segreto donna uomo casa storia",
            "हिंदी कहानी",
        ):
            with self.subTest(value=value):
                result = detect_language(value)
                self.assertEqual(result.code, "unknown")
                self.assertEqual(result.confidence, 0.0)

    def test_unsupported_alphabet_is_classified_as_other(self) -> None:
        manuscript = (
            "\u041f\u0440\u043e\u0448\u043b\u043e\u0439 \u043d\u043e\u0447\u044c\u044e \u0437\u0430\u0437\u0432\u043e\u043d\u0438\u043b \u0442\u0435\u043b\u0435\u0444\u043e\u043d, \u043a\u043e\u0433\u0434\u0430 \u044f \u0441\u043e\u0431\u0438\u0440\u0430\u043b\u0430\u0441\u044c \u0441\u043f\u0430\u0442\u044c. "
            "\u0416\u0435\u043d\u0449\u0438\u043d\u0430 \u0441\u043a\u0430\u0437\u0430\u043b\u0430, \u0447\u0442\u043e \u0437\u043d\u0430\u0435\u0442 \u043c\u043e\u0435\u0433\u043e \u043c\u0443\u0436\u0430, \u0438 \u0441\u043f\u0440\u043e\u0441\u0438\u043b\u0430, \u043f\u043e\u0447\u0435\u043c\u0443 \u044f \u043d\u0438\u043a\u043e\u0433\u0434\u0430 "
            "\u043d\u0435 \u0437\u0430\u043c\u0435\u0447\u0430\u043b\u0430 \u0437\u0430\u043f\u0435\u0440\u0442\u0443\u044e \u043a\u043e\u043c\u043d\u0430\u0442\u0443."
        )
        result = detect_language(manuscript)
        self.assertEqual(result.code, "other")
        self.assertGreaterEqual(result.confidence, 0.64)

    def test_balanced_same_script_languages_are_marked_mixed(self) -> None:
        manuscript = (
            "The woman said that my husband was with her, and I thought it was a joke. "
            "She asked why I never knew the truth, but then the line went silent. "
        ) * 2 + (
            "Ella estaba con mi marido porque él había dicho que nunca volvería. "
            "Ella preguntó por qué yo no sabía nada, pero después respondió que todo había terminado. "
        ) * 3

        result = detect_language(manuscript)
        self.assertEqual(result.code, "mixed")
        self.assertGreaterEqual(result.confidence, 0.64)

    def test_language_aliases_normalize_for_manual_correction(self) -> None:
        self.assertEqual(normalize_language_code("en-US"), "en")
        self.assertEqual(normalize_language_code("zh-CN"), "zh-Hans")
        self.assertEqual(normalize_language_code("zh-TW"), "zh-Hant")
        self.assertEqual(normalize_language_code("it-IT"), "it")
        self.assertEqual(normalize_language_code("ita"), "it")
        self.assertEqual(normalize_language_code("italiano"), "it")
        self.assertEqual(normalize_language_code("hi-IN"), "hi")
        self.assertEqual(normalize_language_code("hin"), "hi")
        self.assertEqual(normalize_language_code("हिन्दी"), "hi")
        self.assertEqual(normalize_language_code("印地语"), "hi")
        self.assertEqual(normalize_language_code("auto", allow_auto=True), "auto")
        with self.assertRaises(ValueError):
            normalize_language_code("auto")


if __name__ == "__main__":
    unittest.main()
