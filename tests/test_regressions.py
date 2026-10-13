import unittest

from core.ai_engine import AIEngine


class RepetitionFilterTests(unittest.TestCase):
    def setUp(self):
        self.engine = AIEngine.__new__(AIEngine)

    def test_rejects_literal_echo_from_audio(self):
        source = "eu vou tentar levar essa torre aqui velho"
        candidate = {
            "texto": "mano eu vou tentar levar essa torre aqui velho",
            "tipo": "reacao", "gancho": "levar torre",
            "naturalidade": 8, "relevancia": 8,
        }
        chosen, notes = self.engine.escolher_candidato(
            [candidate], [], [], eventos_recentes=[
                {"event_type": "audio", "text": source}
            ]
        )
        self.assertIsNone(chosen)
        self.assertTrue(any("eco literal" in note for note in notes))

    def test_keeps_distinct_candidate(self):
        candidate = {
            "texto": "essa fight virou muito rápido",
            "tipo": "reacao", "gancho": "fight",
            "naturalidade": 8, "relevancia": 8,
        }
        chosen, _ = self.engine.escolher_candidato(
            [candidate], [], [], eventos_recentes=[
                {"event_type": "chat", "text": "o time precisa controlar o dragão"}
            ]
        )
        self.assertIsNotNone(chosen)


if __name__ == "__main__":
    unittest.main()
