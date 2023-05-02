import gevent.monkey
gevent.monkey.patch_all()

import bson
import copy
import datetime
import flask
import flask_compress
import flask_cors
import json
import os
import random
import pymongo


class App(flask.Flask):

    @property
    def db(self):
        if not hasattr(self, "_db"):
            self._db = self._get_db()
        return self._db

    def _get_db(self):
        print("Connecting to the database")
        import pymongo
        db = pymongo.MongoClient(os.environ["MONGODB_URI"]).get_default_database()
        return db


app = App(__name__)
flask_cors.CORS(app)
flask_compress.Compress(app)


@app.route("/")
def hello():
    return "Hello world"


@app.route("/decks")
def list_decks():
    password = flask.request.args.get("p")
    decks = list(app.db.decks.find({"user_id": password}))
    now = datetime.datetime.utcnow()

    return flask.jsonify([
        {
            "id": str(deck["_id"]),
            "language": deck["language"],
            "title": deck["title"],
            "ordered": deck.get("ordered") or False,
            "card_count": app.db.cards.count_documents({"deck_id": deck["_id"]}),
            "new_card_count": app.db.cards.count_documents({"deck_id": deck["_id"], "is_new": True}),
            "due_card_count": app.db.cards.count_documents({"deck_id": deck["_id"], "is_new": False, "due": {"$lte": now}}),
            "_links": {
                "deck": flask.url_for("get_deck", deck_id=deck["_id"], _external=True),
            },
        }
        for deck in decks
    ])


@app.route("/decks/<deck_id>")
def get_deck(deck_id):
    deck = app.db.decks.find_one({"_id": bson.ObjectId(deck_id)})
    cards  = list(app.db.cards.find({"deck_id": deck["_id"]}))

    return flask.jsonify({
        "id": str(deck_id),
        "language": deck["language"],
        "title": deck["title"],
        "ordered": deck.get("ordered") or False,
        "card_count": len(cards),
        "cards": [
            _card_dto(card)
            for card in cards
        ]
    })


@app.route("/decks/<deck_id>/cards", methods=["POST"])
def add_card(deck_id):
    request_json = json.loads(flask.request.data)

    card_id = app.db.cards.save({
        "deck_id": bson.ObjectId(deck_id),
        "front": request_json["front"],
        "back": request_json["back"],
        "sound_uri": request_json.get("sound_uri"),
        "image_uri": request_json.get("image_uri"),
        "reverse": request_json.get("reverse"),
        "ordering": _next_ordering(deck_id),
        "is_new": True,
        "created_at": datetime.datetime.utcnow(),
        "updated_at": datetime.datetime.utcnow(),
        "expedited": False,
    })

    return _updated_card(deck_id, card_id)


def _next_ordering(deck_id):
    max_card = app.db.cards.find_one(sort=[("ordering", -1)])
    if not max_card:
        return 0
    return max_card["ordering"] + 10000


@app.route("/decks/<deck_id>/cards/<card_id>", methods=["PATCH"])
def update_card(deck_id, card_id):
    spec = {"_id": bson.ObjectId(card_id), "deck_id": bson.ObjectId(deck_id)}
    request_json = json.loads(flask.request.data)
    request_json["updated_at"] = datetime.datetime.utcnow()

    app.db.cards.update_one(
        spec,
        {"$set": {
            k: v
            for k, v in request_json.items()
            if k in ("front", "back", "sound_uri", "image_uri", "reverse", "updated_at")
        }}
    )

    return _updated_card(deck_id, card_id)


@app.route("/decks/<deck_id>/cards/<card_id>", methods=["DELETE"])
def remove_card(deck_id, card_id):
    spec = {"_id": bson.ObjectId(card_id), "deck_id": bson.ObjectId(deck_id)}
    app.db.cards.remove_one(spec)
    return ('', 204)


@app.route("/decks/<deck_id>/study", methods=["GET"])
def get_study_session(deck_id):
    deck_id = bson.ObjectId(deck_id)
    deck = app.db.decks.find_one({"_id": deck_id})
    now = datetime.datetime.utcnow()

    cards = list(app.db.cards.find({"deck_id": deck_id}, sort=[("ordering", 1)]))
    new_cards = _compute_new_cards(cards)

    if deck.get("ordered"):
        new_cards = sorted(new_cards, key=lambda card: (-1 if card.get("expedited") else 0, card["ordering"]))
    else:
        new_cards = _block_randomize(new_cards, block_size=2000)

    due_cards = _block_randomize(
        [card for card in cards if card.get("due") and card["due"] <= now],
        block_size=10,
    )

    def with_timings(card):
        dto = _card_dto(card)

        for scenario, info in _srs_decision_tree(card).items():
            if scenario == "current_state":
                continue
            dto["interval_if_" + scenario] = info["interval"].total_seconds()

        return dto

    return flask.jsonify({
        "new_cards": [with_timings(card) for card in new_cards],
        "due_cards": [with_timings(card) for card in due_cards],
        "due_distribution": _compute_due_distribution(cards),
        "workload_prediction": _compute_workload(cards),
        "graphs": _compute_graphs(deck_id),
    })


def _compute_new_cards(cards):
    unlocked_cards = {card["_id"] for card in cards if card.get("srs_level", 0) >= 4 or card["expedited"]}

    return [
        card
        for card in cards
        if card["is_new"] and all(dep in unlocked_cards for dep in card.get("depends_on", ()))
    ]


def _compute_due_distribution(cards):
    now = datetime.datetime.utcnow()
    tzoffset = datetime.timedelta(hours=7)

    eod = (now - tzoffset)
    eod = eod.replace(hour=0, minute=0, second=0, microsecond=0)
    eod += datetime.timedelta(days=1)
    eod += tzoffset

    _1d = datetime.timedelta(days=1)
    _3d = datetime.timedelta(days=3)
    _7d = datetime.timedelta(days=7)
    _14d = datetime.timedelta(days=14)
    _30d = datetime.timedelta(days=30)

    def due(lte=datetime.datetime(3000, 1, 1), gt=datetime.datetime(1970, 1, 1)):
        return sum(
            1
            for card in cards
            if card.get("due")
            if gt < card["due"] <= lte
        )

    buckets = [
        {"bucket": "new", "count": len(_compute_new_cards(cards))},
        {"bucket": "now", "count": due(lte=now)},
        {"bucket": "today", "count":  due(gt=now, lte=eod)},
        {"bucket": "tomorrow", "count": due(gt=eod, lte=eod + _1d)},
        {"bucket": "2d-3d", "count": due(gt=eod + _1d, lte=eod + _3d)},
        {"bucket": "4d-1w", "count": due(gt=eod + _3d, lte=eod + _7d)},
        {"bucket": "1w-2w", "count": due(gt=eod + _7d, lte=eod + _14d)},
        {"bucket": "2w-1m", "count": due(gt=eod + _14d, lte=eod + _30d)},
        {"bucket": "1m+", "count": due(gt=eod + _30d)},
    ]

    expedited = sum(1 for c in cards if c["is_new"] and c.get("expedited"))
    if expedited:
        buckets.insert(1, {"bucket": "expedited", "count": expedited})
        buckets[0]["count"] -= buckets[1]["count"]
    return buckets


def _compute_workload(cards):
    cards = copy.copy(cards)
    now = datetime.datetime.utcnow()
    tzoffset = datetime.timedelta(hours=4)

    eod = (now - tzoffset)
    eod = eod.replace(hour=0, minute=0, second=0, microsecond=0)
    eod += datetime.timedelta(days=1)
    eod += tzoffset

    studied_per_day = []
    for i in range(7):
        studied_this_day = 0
        for j, card in enumerate(cards):
            if card.get("due") and card["due"] <= eod:
                studied_this_day += 1
                cards[j] = dict(card, due=_srs_decision_tree(card, now=eod)["right"]["updates"]["$set"]["due"])
        studied_per_day.append({"x": (eod - datetime.timedelta(days=1)).isoformat()[0:10], "y": studied_this_day})
        eod += datetime.timedelta(days=1)

    return studied_per_day


def _compute_graphs(deck_id):
    map_fn = bson.Code("""\
        function() {
            var date = (new Date(this.timestamp.getTime() - 7 * 3600 * 1000).toISOString().substring(0, 10));
            if (this.srs_level == null) {
                emit(date + '::New Cards', {correct: 1, answers: 1});
            } else {
                var srsLevelGroup = '?';
                if (this.srs_level <= 2) {
                    srsLevelGroup = 'Very Imature Cards (SRS Level 0-2)';
                } else if (this.srs_level <= 4) {
                    srsLevelGroup = 'Imature Cards (SRS Level 3-4)';
                } else if (this.srs_level <= 7) {
                    srsLevelGroup = 'Almost Mature Cards (SRS Level 5-7)';
                } else {
                    srsLevelGroup = 'Mature Cards (SRS Level 8+)';
                }
                emit(date + '::Recall Rate/' + srsLevelGroup, {correct: (this.scenario == 'wrong') ? 0 : 1, answers: 1});
                emit(date + '::Reviewed Cards', {correct: (this.scenario == 'wrong') ? 0 : 1, answers: 1});
            }
        }
    """)

    reduce_fn = bson.Code("""\
        function(key, values) {
            return {
                correct: Array.sum(values.map(function(v) { return v.correct; })),
                answers: Array.sum(values.map(function(v) { return v.answers; }))
            };
        }
    """)

    stats = {}
    #stats = app.db.answer_log.map_reduce(
    #    map_fn,
    #    reduce_fn,
    #    {"inline": True},
    #    full_response=True,
    #    query={"deck_id": deck_id, "timestamp": {"$gte": datetime.datetime.utcnow() - datetime.timedelta(days=90)}},
    #)["results"]

    def push_to_series(series_name, date, value):
        series.setdefault(series_name, [])
        series[series_name].append({"x": date, "y": value})

    series = {}
    for item in stats:
        date, series_name = item["_id"].split("::")
        if series_name == "New Cards":
            push_to_series(series_name, date, item["value"]["answers"])
        elif series_name == "Reviewed Cards":
            push_to_series(series_name, date, item["value"]["answers"])
            push_to_series("Recall Rate/All Cards", date, item["value"]["correct"] / item["value"]["answers"])
        else:
            push_to_series(series_name, date, item["value"]["correct"] / item["value"]["answers"])

    return [
        {
            "name": series_name,
            "data": sorted(values, key=lambda v: v["x"]),
        }
        for series_name, values in series.items()
    ]


@app.route("/answers", methods=["POST"])
def post_study_answers():
    request_json = json.loads(flask.request.data)
    session_id = flask.request.args["session_id"]

    parsed_answers = []

    for answer in request_json:
        card_id = bson.ObjectId(answer["card_id"])
        if answer["scenario"] not in ("right", "easy", "wrong"):
            flask.abort(400)

        scenario = answer["scenario"]
        timestamp = datetime.datetime.utcfromtimestamp(answer["timestamp"])

        parsed_answers.append({
            "card_id": card_id,
            "scenario": scenario,
            "timestamp": timestamp,
        })

    cards_by_id = {
        card["_id"]: card
        for card in app.db.cards.find({
            "_id": {"$in": [answer["card_id"] for answer in parsed_answers]},
        })
    }

    cards_bulk = []
    log_bulk = []

    for answer in sorted(parsed_answers, key=lambda ans: ans["timestamp"]):
        card = cards_by_id[answer["card_id"]]
        decision_tree = _srs_decision_tree(card)
        updates = decision_tree[answer["scenario"]]["updates"]

        if not updates:
            continue

        cards_bulk.append(pymongo.UpdateOne({"_id": card["_id"]}, updates))
        log_bulk.append(pymongo.InsertOne(dict({
            "session_id": session_id,
            "deck_id": card["deck_id"],
            "card_id": card["_id"],
            "scenario": answer["scenario"],
            "timestamp": answer["timestamp"],
        }, **decision_tree["current_state"])))

    app.db.cards.bulk_write(cards_bulk)
    app.db.answer_log.bulk_write(log_bulk, ordered=False)

    return ('', 204)


def _updated_card(deck_id, card_id):
    spec = {"_id": bson.ObjectId(card_id), "deck_id": bson.ObjectId(deck_id)}
    card = app.db.cards.find_one({
        "deck_id": bson.ObjectId(deck_id),
        "_id": bson.ObjectId(card_id)
    })

    if not card:
        flask.abort(404)

    return flask.jsonify(_card_dto(card))


def _srs_decision_tree(card, now=None):
    NOW = now or datetime.datetime.utcnow()
    SRS_LEVELS = [
        datetime.timedelta(minutes=10),         # 0
        datetime.timedelta(hours=1),            # 1
        datetime.timedelta(hours=4),            # 2
        datetime.timedelta(days=1),             # 3
        datetime.timedelta(days=2),             # 4
        datetime.timedelta(days=3),             # 5
        datetime.timedelta(days=5),             # 6
        datetime.timedelta(days=8),             # 7
        datetime.timedelta(days=13),            # 8
        datetime.timedelta(days=20),            # 9
        datetime.timedelta(days=40),            # 10
        datetime.timedelta(days=80),            # 11
        datetime.timedelta(days=100),           # 12
        datetime.timedelta(days=180),           # 13
        datetime.timedelta(days=365),           # 14
    ] if card["reverse"] else [
        datetime.timedelta(minutes=10),         # 0
        datetime.timedelta(hours=1),            # 1
        datetime.timedelta(hours=4),            # 2
        datetime.timedelta(days=1),             # 3
        datetime.timedelta(days=2),             # 4
        datetime.timedelta(days=4),             # 5
        datetime.timedelta(days=6),             # 6
        datetime.timedelta(days=9),             # 7
        datetime.timedelta(days=15),            # 8
        datetime.timedelta(days=21),            # 9
        datetime.timedelta(days=40),            # 10
        datetime.timedelta(days=80),            # 11
        datetime.timedelta(days=100),           # 12
        datetime.timedelta(days=180),           # 13
        datetime.timedelta(days=365),           # 14
    ]
    SRS_INITIAL_LEVEL = 3

    if card["is_new"]:
        right_srs_level = 3
        easy_srs_level = 4
    else:
        card_srs_level = card["srs_level"]
        right_srs_level = min(card_srs_level + 1, len(SRS_LEVELS) - 1)
        easy_srs_level = min(card_srs_level + 2, len(SRS_LEVELS) - 1)
        if card_srs_level >= 11:
            wrong_srs_level = card_srs_level - 3
        elif card_srs_level >= 7:
            wrong_srs_level = card_srs_level - 2
        else:
            wrong_srs_level = max(card_srs_level - 1, 0)
        time_since_last_saw = NOW - card["last_answered"]
        if time_since_last_saw > SRS_LEVELS[right_srs_level]:
            right_srs_level = min(right_srs_level + 1, len(SRS_LEVELS) - 1)
            easy_srs_level = min(easy_srs_level + 1, len(SRS_LEVELS) - 1)

    def adjust_hour(timestamp):
        if NOW.hour < 7:
            eod_today = NOW.replace(hour=7, minute=0, second=0, microsecond=0)
        else:
            eod_today = NOW.replace(hour=7, minute=0, second=0, microsecond=0)
            eod_today += datetime.timedelta(days=1)

        if timestamp < eod_today:
            return timestamp

        adjusted = timestamp.replace(hour=7, minute=1, second=0, microsecond=0)
        if adjusted > timestamp:
            return adjusted - datetime.timedelta(days=1)

        return adjusted

    possibilities = {
        "right": {
            "interval": SRS_LEVELS[right_srs_level],
            "updates": {
                "$set": {
                    "due": adjust_hour(NOW + SRS_LEVELS[right_srs_level]),
                    "srs_level": right_srs_level,
                    "is_new": False,
                    "hit_ratio": (card.get("hits", 0) + 1) / (card.get("answers", 0) + 1),
                    "last_answered": NOW,
                },
                "$inc": {
                    "hits": 1,
                    "answers": 1,
                },
            },
        },
        "easy": {
            "interval": SRS_LEVELS[easy_srs_level],
            "updates": {
                "$set": {
                    "due": adjust_hour(NOW + SRS_LEVELS[easy_srs_level]),
                    "srs_level": easy_srs_level,
                    "is_new": False,
                    "hit_ratio": (card.get("hits", 0) + 1) / (card.get("answers", 0) + 1),
                    "last_answered": NOW,
                },
                "$inc": {
                    "hits": 1,
                    "answers": 1,
                },
            },
        },
        "current_state": {
            "due": card.get("due", None),
            "is_new": card["is_new"],
            "srs_level": card.get("srs_level", None),
            "answers": card.get("answers", 0),
            "hits": card.get("hits", 0),
            "hit_ratio": card.get("hit_ratio", None),
            "last_answered": card.get("last_answered", None),
        },
    }

    if not card["is_new"]:
        possibilities["wrong"] = {
            "interval": SRS_LEVELS[wrong_srs_level],
            "updates": {
                "$set": {
                    "due": adjust_hour(NOW + SRS_LEVELS[wrong_srs_level]),
                    "srs_level": wrong_srs_level,
                    "is_new": False,
                    "hit_ratio": (card.get("hits", 0)) / (card.get("answers", 0) + 1),
                    "last_answered": NOW,
                },
                "$inc": {
                    "answers": 1,
                }
            },
        }

    return possibilities


def _card_dto(card):
    sound_uri = card.get("sound_uri")
    if not sound_uri and card.get("sound_uris"):
        sound_uri = card["sound_uris"][0]["uri"]

    base_dto = dict({
        "id": str(card["_id"]),
        "front": card["front"],
        "back": card["back"],
        "type": card.get("type", "default"),
        "depends_on": [str(dep) for dep in card.get("depends_on", ())],
        "sound_uri": sound_uri,
        "image_uri": card.get("image_uri"),
        "reverse": card.get("reverse", False),
        "ordering": card.get("ordering", 0),
        "created_at": card["created_at"],
        "updated_at": card["updated_at"],
    }, **_srs_decision_tree(card)["current_state"])

    if card.get("type", "default").startswith("wanikani"):
        for key in ("reading_mnemonic", "meaning_mnemonic", "name_mnemonic", "context_sentences", "level", "sound_uris"):
            if key in card:
                base_dto[key] = card[key]

    return base_dto


def _block_randomize(cards, block_size):
    # First, order by due date violation (relative)
    now = datetime.datetime.utcnow()

    def due_date_violation(card):
        if not card.get("last_answered"):
            return 1.0

        elapsed_time = now - card["last_answered"]
        target_elapsed_time = card["due"] - card["last_answered"]
        return elapsed_time.total_seconds() / target_elapsed_time.total_seconds()

    cards = sorted(cards, key=due_date_violation, reverse=True)
    sorted_cards = []

    for block_start in range(0, len(cards), block_size):
        block = cards[block_start:block_start + block_size]
        random.shuffle(block)
        sorted_cards += block

    return sorted_cards
