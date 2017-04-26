import bson
import flask
import flask_cors
import json
import os


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
            {
                "id": str(card["_id"]),
                "front": card["front"],
                "back": card["back"],
                "reverse": card.get("reverse", False),
                "image_uri": card.get("image_uri"),
                "sound_uri": card.get("sound_uri"),
            }
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
    })

    return _updated_card(deck_id, card_id)


@app.route("/decks/<deck_id>/cards/<card_id>", methods=["PATCH"])
def update_card(deck_id, card_id):
    spec = {"_id": bson.ObjectId(card_id), "deck_id": bson.ObjectId(deck_id)}
    request_json = json.loads(flask.request.data)

    app.db.cards.update(
        spec,
        {"$set": {
            k: v
            for k, v in request_json.iteritems()
            if k in ("front", "back", "sound_uri", "image_uri", "reverse")
        }}
    )

    return _updated_card(deck_id, card_id)


@app.route("/decks/<deck_id>/cards/<card_id>", methods=["DELETE"])
def remove_card(deck_id, card_id):
    spec = {"_id": bson.ObjectId(card_id), "deck_id": bson.ObjectId(deck_id)}
    app.db.cards.remove(spec)
    return ('', 204)


def _updated_card(deck_id, card_id):
    spec = {"_id": bson.ObjectId(card_id), "deck_id": bson.ObjectId(deck_id)}
    card = app.db.cards.find_one({
        "deck_id": bson.ObjectId(deck_id),
        "_id": bson.ObjectId(card_id)
    })

    if not card:
        flask.abort(404)

    return flask.jsonify({
        "id": str(card["_id"]),
        "front": card["front"],
        "back": card["back"],
        "sound_uri": card.get("sound_uri"),
        "image_uri": card.get("image_uri"),
        "reverse": card.get("reverse", False),
    })
