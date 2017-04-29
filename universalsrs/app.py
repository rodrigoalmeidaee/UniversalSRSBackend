from __future__ import division

import bson
import datetime
import flask
import flask_cors
import json
import os
import random


class App(flask.Flask):

    @property
    def db(self):
        if not hasattr(self, "_db"):
            self._db = self._get_db()
        return self._db

    def _get_db(self):
        print "Connecting to the database"
        import pymongo
        db = pymongo.MongoClient(os.environ["MONGODB_URI"]).get_default_database()
        return db


app = App(__name__)
flask_cors.CORS(app)


@app.route("/")
def hello():
    return "Hello world"


@app.route("/decks")
def list_decks():
    decks = list(app.db.decks.find())
    return flask.jsonify([
        {
            "id": str(deck["_id"]),
            "language": deck["language"],
            "title": deck["title"],
            "card_count": app.db.cards.find({"deck_id": deck["_id"]}).count(),
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

    app.db.cards.update(
        spec,
        {"$set": {
            k: v
            for k, v in request_json.iteritems()
            if k in ("front", "back", "sound_uri", "image_uri", "reverse", "updated_at")
        }}
    )

    return _updated_card(deck_id, card_id)


@app.route("/decks/<deck_id>/cards/<card_id>", methods=["DELETE"])
def remove_card(deck_id, card_id):
    spec = {"_id": bson.ObjectId(card_id), "deck_id": bson.ObjectId(deck_id)}
    app.db.cards.remove(spec)
    return ('', 204)


@app.route("/decks/<deck_id>/study", methods=["GET"])
def get_study_session(deck_id):
    deck_id = bson.ObjectId(deck_id)
    now = datetime.datetime.utcnow()

    new_cards = _block_randomize(
        app.db.cards.find({"deck_id": deck_id, "is_new": True}),
        block_size=20
    )
    due_cards = _block_randomize(
        app.db.cards.find({"deck_id": deck_id, "srs_due": {"$lte": now}}),
        block_size=10,
    )

    def with_timings(card):
        dto = _card_dto(card)

        for scenario, info in _srs_decision_tree(card).iteritems():
            if scenario == "current_state":
                continue
            dto["interval_if_" + scenario] = info["interval"].total_seconds()

        return dto

    return flask.jsonify({
        "new_cards": [with_timings(card) for card in new_cards],
        "due_cards": [with_timings(card) for card in due_cards],
        "due_distribution": _compute_due_distribution(deck_id)
    })


def _compute_due_distribution(deck_id):
    now = datetime.datetime.utcnow()
    tzoffset = datetime.timedelta(hours=7)

    due = lambda **spec: {"deck_id": deck_id, "is_new": False, "due": {"$" + key: value for key, value in spec.iteritems()}}

    eod = (now - tzoffset)
    eod = eod.replace(hour=0, minute=0, second=0, microsecond=0)
    eod += datetime.timedelta(days=1)
    eod += tzoffset


    _1d = datetime.timedelta(days=1)
    _3d = datetime.timedelta(days=3)
    _7d = datetime.timedelta(days=7)
    _14d = datetime.timedelta(days=14)
    _30d = datetime.timedelta(days=30)

    print [now, eod, eod + _1d]

    return [
        {"bucket": "new", "count": app.db.cards.count({"deck_id": deck_id, "is_new": True})},
        {"bucket": "now", "count": app.db.cards.count(due(lte=now))},
        {"bucket": "today", "count":  app.db.cards.count(due(gt=now, lte=eod))},
        {"bucket": "tomorrow", "count": app.db.cards.count(due(gt=eod, lte=eod + _1d))},
        {"bucket": "2d-3d", "count": app.db.cards.count(due(gt=eod + _1d, lte=eod + _3d))},
        {"bucket": "4d-1w", "count": app.db.cards.count(due(gt=eod + _3d, lte=eod + _7d))},
        {"bucket": "1w-2w", "count": app.db.cards.count(due(gt=eod + _7d, lte=eod + _14d))},
        {"bucket": "2w-1m", "count": app.db.cards.count(due(gt=eod + _14d, lte=eod + _30d))},
        {"bucket": "1m+", "count": app.db.cards.count(due(gt=eod + _30d))},
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

    cards_bulk = app.db.cards.initialize_ordered_bulk_op()
    log_bulk = app.db.answer_log.initialize_unordered_bulk_op()

    for answer in sorted(parsed_answers, key=lambda ans: ans["timestamp"]):
        card = cards_by_id[answer["card_id"]]
        decision_tree = _srs_decision_tree(card)
        updates = decision_tree[answer["scenario"]]["updates"]

        if not updates:
            continue

        cards_bulk.find({"_id": card["_id"]}).update(updates)
        log_bulk.insert(dict({
            "session_id": session_id,
            "deck_id": card["deck_id"],
            "card_id": card["_id"],
            "scenario": answer["scenario"],
            "timestamp": answer["timestamp"],
        }, **decision_tree["current_state"]))

    cards_bulk.execute()
    log_bulk.execute()

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


def _srs_decision_tree(card):
    NOW = datetime.datetime.utcnow()
    SRS_LEVELS = [
        datetime.timedelta(minutes=10),
        datetime.timedelta(hours=1),
        datetime.timedelta(hours=4),
        datetime.timedelta(days=1, hours=-4),
        datetime.timedelta(days=2, hours=-4),
        datetime.timedelta(days=3, hours=-4),
        datetime.timedelta(days=5, hours=-4),
        datetime.timedelta(days=8, hours=-4),
        datetime.timedelta(days=13, hours=-4),
        datetime.timedelta(days=20, hours=-4),
        datetime.timedelta(days=40, hours=-4),
        datetime.timedelta(days=80, hours=-4),
    ]
    SRS_INITIAL_LEVEL = 2

    if card["is_new"]:
        right_srs_level = 2
        easy_srs_level = 3
    else:
        card_srs_level = card["srs_level"]
        right_srs_level = min(card_srs_level + 1, len(SRS_LEVELS) - 1)
        easy_srs_level = min(card_srs_level + 2, len(SRS_LEVELS) - 1)
        wrong_srs_level = max(card_srs_level - 1, 0)

    possibilities = {
        "right": {
            "interval": SRS_LEVELS[right_srs_level],
            "updates": {
                "$set": {
                    "due": NOW + SRS_LEVELS[right_srs_level],
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
                    "due": NOW + SRS_LEVELS[easy_srs_level],
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
                    "due": NOW + SRS_LEVELS[wrong_srs_level],
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
    return dict({
        "id": str(card["_id"]),
        "front": card["front"],
        "back": card["back"],
        "sound_uri": card.get("sound_uri"),
        "image_uri": card.get("image_uri"),
        "reverse": card.get("reverse", False),
        "ordering": card.get("ordering", 0),
        "created_at": card["created_at"],
        "updated_at": card["updated_at"],
    }, **_srs_decision_tree(card)["current_state"])


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

    for block_start in xrange(0, len(cards), block_size):
        block = cards[block_start:block_start + block_size]
        random.shuffle(block)
        sorted_cards += block

    return sorted_cards
