import json
import os
import tempfile
import time
import unittest

import bot


class BotTests(unittest.TestCase):
    def test_evaluate_flash_deal_thresholds(self):
        self.assertEqual(bot.evaluate_flash_deal("50% OFF", 40, 100)[0], True)
        self.assertEqual(bot.evaluate_flash_deal("30% OFF", 40, 100)[0], False)
        self.assertEqual(bot.evaluate_flash_deal("₹160 OFF", 40, 100)[0], True)
        self.assertEqual(bot.evaluate_flash_deal("₹60 OFF", 40, 100)[0], False)
        self.assertEqual(bot.evaluate_flash_deal("BOGO Special", 40, 100)[0], True)
        self.assertEqual(bot.evaluate_flash_deal("", 40, 100), (False, 0))

    def test_parse_zomato_page_payload(self):
        sample = {
            "page_data": {
                "sections": {
                    "SECTION_SEARCH_RESULT": [
                        {"type": "banner"},
                        {
                            "info": {
                                "resId": 12345,
                                "name": "Sharief Bhai Biryani",
                                "locality": {"name": "BTM, Bangalore"},
                                "rating": {"aggregate_rating": "4.3"},
                                "cft": {"text": "₹400 for two"},
                                "cuisine": [{"name": "Biryani"}, {"name": "Mughlai"}],
                            },
                            "order": {"deliveryTime": "25 min"},
                            "bulkOffers": [{"text": "50% OFF"}],
                            "gold": [],
                            "cardAction": {"clickUrl": "/bangalore/sharief-bhai-btm/order"},
                        },
                        {
                            "info": {
                                "resId": 99999,
                                "name": "Normal Cafe",
                                "locality": {"name": "BTM"},
                                "rating": {"aggregate_rating": "4.0"},
                            },
                            "bulkOffers": [{"text": "20% OFF"}],
                            "gold": {},
                        },
                    ]
                }
            }
        }
        deals = bot.parse_zomato_page_payload(sample, min_percent_off=40, min_flat_off=100)
        self.assertEqual(len(deals), 2)
        self.assertTrue(deals[0]["is_flash"])
        self.assertEqual(deals[0]["discount"], "50% OFF")
        self.assertEqual(deals[0]["url"], "https://www.zomato.com/bangalore/sharief-bhai-btm/order")
        self.assertFalse(deals[1]["is_flash"])

    def test_parse_swiggy_offers_html(self):
        next_data = {
            "props": {
                "pageProps": {
                    "widgetResponse": {
                        "success": {
                            "cards": [
                                {
                                    "card": {
                                        "card": {
                                            "gridElements": {
                                                "infoWithStyle": {
                                                    "restaurants": [
                                                        {
                                                            "info": {
                                                                "id": "555",
                                                                "name": "Brik Oven",
                                                                "locality": "Koramangala",
                                                                "rating": {"avgRating": "4.5"},
                                                                "cuisines": ["Pizza"],
                                                                "costForTwo": "₹900 for two",
                                                                "offerInfoV2": {
                                                                    "customerOffer": {
                                                                        "header": "₹125 off with PAYTMNEW"
                                                                    }
                                                                },
                                                                "customerOffer": {
                                                                    "loyaltyInfo": {
                                                                        "offerDetail": {
                                                                            "title": "₹150 Off",
                                                                            "subtitle": "above ₹1500",
                                                                        }
                                                                    }
                                                                },
                                                            }
                                                        }
                                                    ]
                                                }
                                            }
                                        }
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        }
        html_doc = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></html>'
        deals = bot.parse_swiggy_offers_html(html_doc, min_percent_off=40, min_flat_off=100)
        self.assertEqual(len(deals), 1)
        self.assertTrue(deals[0]["is_flash"])
        self.assertIn("₹150 Off", deals[0]["discount"])

    def test_state_persistence_and_ttl_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state", "bot_state.json")
            now = time.time()
            state = {
                "seen_deals": {
                    "fresh": now - 60,
                    "expired": now - (bot.DEAL_TTL_SECONDS + 60),
                },
                "last_update_id": 42,
                "chat_id": "12345",
            }
            bot.save_state(state_path, state)
            loaded = bot.load_state(state_path)
            pruned = bot.prune_seen_deals(loaded["seen_deals"], now_ts=now)
            self.assertIn("fresh", pruned)
            self.assertNotIn("expired", pruned)
            self.assertEqual(loaded["last_update_id"], 42)

    def test_handle_deals_command_uses_cached_deals(self):
        cfg = {
            "location_name": "BTM 2nd Stage, Bangalore",
            "min_percent_off": 40,
            "min_flat_off": 100,
            "zomato_mqtt_conf": {},
        }
        state = {"seen_deals": {}, "last_zomato_count": 1, "last_swiggy_count": 0}
        cached = [
            {
                "source": "zomato",
                "id": "1",
                "name": "Pizza Hut",
                "area": "BTM",
                "rating": "4.1",
                "sla": "25 min",
                "cost": "₹500 for two",
                "cuisines": "Pizza",
                "discount": "50% OFF",
                "url": "https://www.zomato.com/test",
                "is_flash": True,
                "score": 150,
            }
        ]
        reply, _ = bot.handle_command("/deals", "Girish", "123", cfg, state, cached_deals=cached)
        self.assertIn("Pizza Hut", reply)
        self.assertIn("50% OFF", reply)


if __name__ == "__main__":
    unittest.main()
