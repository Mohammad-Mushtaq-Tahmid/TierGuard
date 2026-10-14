import unittest
from tierguard.policy.confirm import ConfirmationService, action_hash


class Clock:
    t = 1000.0
    def __call__(self): return self.t


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.c = ConfirmationService(ttl=120, clock=self.clock)
        self.h = action_hash("C001", "transfer", {"payee_id": "P001", "amount": "500.00"})

    def test_happy_path_single_use(self):
        ch = self.c.issue("s1", self.h, False)
        self.assertEqual(self.c.redeem("s1", ch.token, self.h), (True, "OK"))
        self.assertEqual(self.c.redeem("s1", ch.token, self.h), (False, "REPLAYED_TOKEN"))

    def test_changed_action_rejected_and_burned(self):
        ch = self.c.issue("s1", self.h, False)
        other = action_hash("C001", "transfer", {"payee_id": "P001", "amount": "5000.00"})
        self.assertEqual(self.c.redeem("s1", ch.token, other), (False, "HASH_MISMATCH"))
        self.assertEqual(self.c.redeem("s1", ch.token, self.h)[0], False)  # burned, cannot retry with the right one

    def test_expiry(self):
        ch = self.c.issue("s1", self.h, False)
        self.clock.t += 121
        self.assertEqual(self.c.redeem("s1", ch.token, self.h), (False, "EXPIRED_TOKEN"))

    def test_other_session_cannot_use_token(self):
        ch = self.c.issue("s1", self.h, False)
        self.assertEqual(self.c.redeem("s2", ch.token, self.h), (False, "SESSION_MISMATCH"))

    def test_step_up_needs_correct_otp(self):
        ch = self.c.issue("s1", self.h, True)
        otp = self.c.sms_outbox[-1]["otp"]
        self.assertEqual(self.c.redeem("s1", ch.token, self.h), (False, "OTP_REQUIRED"))
        wrong = "000000" if otp != "000000" else "111111"
        self.assertEqual(self.c.redeem("s1", ch.token, self.h, wrong), (False, "OTP_WRONG"))
        self.assertEqual(self.c.redeem("s1", ch.token, self.h, otp), (True, "OK"))

    def test_otp_attempts_limited(self):
        ch = self.c.issue("s1", self.h, True)
        otp = self.c.sms_outbox[-1]["otp"]
        wrong = "000000" if otp != "000000" else "111111"
        for _ in range(3):
            self.c.redeem("s1", ch.token, self.h, wrong)
        self.assertEqual(self.c.redeem("s1", ch.token, self.h, otp)[0], False)

    def test_unknown_token(self):
        self.assertEqual(self.c.redeem("s1", "nope", self.h), (False, "UNKNOWN_TOKEN"))

    def test_hash_is_canonical(self):
        a = action_hash("C001", "t", {"a": 1, "b": 2})
        b = action_hash("C001", "t", {"b": 2, "a": 1})
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
