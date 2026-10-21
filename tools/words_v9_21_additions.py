"""V9.21 (20 OCT setup night): the words for the battle matrix, the pile-up
call, the results programme. Run once from the repo root:

    python tools/words_v9_21_additions.py

It adds variants to hoover_words_v4.json (idempotent: a variant whose
present text is already there is skipped). Language rule (20 OCT spec): it
is racing, never a game -- no controllers, buttons, consoles, restarts.
"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(os.path.dirname(HERE), "hoover_words_v4.json")


def V(speaker, present, when=None, **kw):
    d = collections.OrderedDict()
    d["speaker"] = speaker
    if when:
        d["when"] = when
    d["present"] = present
    for k, v in kw.items():
        d[k] = v
    return d


ADD = collections.OrderedDict()

# ---- the pile-up call (INC-01) ---------------------------------------------
ADD["S_INC_01"] = [
    V("LEAD", "Big crash! {count} cars in it{where}, and {a} is right in the middle of that.",
      {"beat": "pileup", "humans": 1, "where": True}),
    V("LEAD", "Oh, that is a big one! {count} cars together at {where}, {a} and {b} both in it.",
      {"beat": "pileup", "humans": 2, "where": True}),
    V("LEAD", "Crash! {count} cars at {where}, and three of ours, {list}, are in it!",
      {"beat": "pileup", "humans": 3, "where": True}),
    V("LEAD", "Big crash! {count} cars in it, and {a} is right in the middle of that.",
      {"beat": "pileup", "humans": 1}),
    V("LEAD", "Oh, that is a big one! {count} cars together, {a} and {b} both in it.",
      {"beat": "pileup", "humans": 2}),
    V("LEAD", "Crash! {count} cars, and {list} are all in it!",
      {"beat": "pileup", "humans": 3}),
    V("LEAD", "Big crash at {where}! {count} cars in that one.",
      {"beat": "pileup", "humans": 0, "where": True}),
    V("LEAD", "Crash! {count} cars together, and that is going to shuffle the order.",
      {"beat": "pileup", "humans": 0}),
    V("LEAD", "Carnage on the opening lap! {count} cars in it, and {a} is one of them.",
      {"beat": "pileup", "first_lap": True, "humans": 1}),
    V("LEAD", "A pile-up, {count} cars. Hold on, we will sort out who is where.",
      {"beat": "pileup"}, fallback=True),
    V("ANALYST", "Out of that pile-up, {a} is the one who paid for it, {places} gone, {pos} now.",
      {"beat": "consequence", "pileup": True, "human": True}),
    V("ANALYST", "The dust settles on that {count} car crash, and {a} has lost {places}.",
      {"beat": "consequence", "pileup": True}),
]
ADD["S_INC_06"] = [
    V("LEAD", "Chaos at the start! {count} cars in trouble on the opening lap.",
      {"beat": "chaos", "big": True}),
    V("ANALYST", "{count} cars caught up in that first lap mess. Let us find our drivers in it.",
      {"beat": "chaos", "human": True}),
]

# ---- the results programme (SF-07) -------------------------------------------
ADD["S_SF_07"] = [
    V("LEAD", "And one of our own is on the podium: {a}, {pos} overall, and that is prize money for {team} and {charity}.",
      {"beat": "human_podium", "win": False, "money": True}),
    V("LEAD", "{a} wins it, one of ours on the top step, and the money goes to {charity}.",
      {"beat": "human_podium", "win": True, "money": True}),
    V("LEAD", "One of our own is on the podium: {a}, {pos} overall, for {team}.",
      {"beat": "human_podium", "has_team": True}),
    V("LEAD", "One of our own is on the podium: {a}, {pos} overall.",
      {"beat": "human_podium"}),
    V("ANALYST", "Best of our drivers tonight: {a}, {pos} at the flag, for {team} and {charity}.",
      {"beat": "best_human", "programme": True, "has_team": True}),
    V("ANALYST", "Best of our drivers tonight is {a}, home in {pos}.",
      {"beat": "best_human", "programme": True}),
    V("LEAD", "The classification for our drivers. {a}, {pos}, for {team} and {charity}.",
      {"beat": "human_result", "first": True, "has_charity": True}),
    V("LEAD", "{a}, {pos}, for {team} and {charity}.",
      {"beat": "human_result", "has_charity": True}),
    V("LEAD", "{a} is classified {pos}, racing for {team} and {charity}.",
      {"beat": "human_result", "has_charity": True}),
    V("LEAD", "{a}, {pos}, for {team}.", {"beat": "human_result", "has_team": True}),
    V("LEAD", "{a} is classified {pos}.", {"beat": "human_result"}),
    V("LEAD", "{a} did not see the flag tonight; his race ended early, and {team} and {charity} will have to wait for the next one.",
      {"beat": "human_out", "has_charity": True}),
    V("LEAD", "{a} did not finish; his race ended early.", {"beat": "human_out"}),
    V("ANALYST", "Pole position in qualifying went to {a}, and that is ten dollars to {charity}.",
      {"beat": "pole", "money": True}),
    V("ANALYST", "Pole position in qualifying was {a}'s.", {"beat": "pole"}),
    V("ANALYST", "Fastest lap of the race: {a}, a {time}, and ten dollars more for {charity}.",
      {"beat": "fastest_lap", "money": True}),
    V("ANALYST", "The fastest lap of the race went to {a}, a {time}.", {"beat": "fastest_lap"}),
    V("LEAD", "The Constructors' Cup, both cars counted: {a} take it on {pa} points, {b} second on {pb}, {c} third on {pc}, and the top three teams earn for their charities.",
      {"beat": "constructors", "money": True}),
    V("LEAD", "The Constructors' Cup: {a} first on {pa} points, {b} second on {pb}, {c} third on {pc}.",
      {"beat": "constructors"}),
    V("ANALYST", "The full results, and the money raised for charity tonight, are on your screen now.",
      {"beat": "handoff", "money": True}),
    V("ANALYST", "The full results are on your screen now.", {"beat": "handoff"}),
]

# ---- battle phases on BAT-01 and LEAD-02 --------------------------------------
PHASES = [
    V("LEAD", "{a} lunges at {b}!", {"beat": "lunge"}),
    V("LEAD", "Here goes {a}, up the inside of {b} for {pos}!", {"beat": "lunge"}),
    V("LEAD", "{a} goes for it on {b}!", {"beat": "lunge"}),
    V("LEAD", "A move from {a}, diving at {b}!", {"beat": "lunge"}),
    V("ANALYST", "{b} shuts the door. {a} will have to find another way through.", {"beat": "defended"}),
    V("ANALYST", "Covered. {b} saw that one coming and {a} has had to back out of it.", {"beat": "defended"}),
    V("ANALYST", "{b} holds {pos}. That is the line covered, and {a} is back behind him.", {"beat": "defended"}),
    V("ANALYST", "No way through for {a}; {b} placed the car exactly where it needed to be.", {"beat": "defended"}),
    V("LEAD", "{a} still right there on {b}, {gap} back, looking for a way past.", {"beat": "detail"}),
    V("ANALYST", "{gap} between them now. {b} is driving a defensive line and {a} is waiting for a mistake.", {"beat": "detail"}),
    V("LEAD", "The gap is {gap}. {a} has the pace; {b} has the position.", {"beat": "detail"}),
    V("ANALYST", "{a} has the wing open on {b} and the gap is down to {gap}.", {"beat": "detail", "drs": True}),
    V("LEAD", "They are nose to tail for {pos}, {a} on {b}, nothing in it.", {"beat": "detail", "close": True}),
    V("ANALYST", "This is a proper fight for {pos}. {b} ahead, {a} {gap} behind and not going anywhere.", {"beat": "detail"}),
]

# ---- the big-battle reactions (spec section 7, all 48 approved) ---------------
R = []
def react(speaker, text, when=None, **kw):
    w = {"beat": "reaction"}
    if when:
        w.update(when)
    R.append(V(speaker, text, w, once=True, **kw))

# Excitement (unnamed unless marked)
react("ANALYST", "Wow. What a fight that was.")
react("LEAD", "These two have gone at it for lap after lap. Absolutely brilliant.")
react("ANALYST", "What a show {a} and {b} are giving us tonight!", {"named": True})
react("LEAD", "That is proper racing. Hard, fair, and right on the limit.")
react("ANALYST", "You don't get much better than that. Wheel to wheel, corner after corner.")
react("LEAD", "The courage from both of them. Not an inch given.")
react("ANALYST", "I've been holding my breath for two laps watching that.")
react("LEAD", "Give them both a round of applause. That was racing at its very best.")
react("ANALYST", "{a} and {b}: remember that one, because we'll be talking about it all night.", {"hh": True})
react("LEAD", "That's what this league is all about, right there.")
react("ANALYST", "Nobody blinked. Nobody backed out. Superb from both of them.")
react("LEAD", "Lap after lap, inches apart. What a contest.")
react("ANALYST", "The respect between those two. Racing that close and both still in one piece.")
react("LEAD", "{b} threw everything at it, and {a} had an answer every time.", {"hh": True})
react("ANALYST", "Edge of your seat stuff. I don't think anyone watching sat back for a second.")
react("LEAD", "Two drivers, one piece of tarmac, and neither of them willing to give it up.")
react("ANALYST", "If that's a taste of the season to come, sign me up.")
react("LEAD", "That battle had everything: the lunge, the defence, the switchback. Spectacular.", {"lunge": True})
react("ANALYST", "Somewhere a charity is cheering, because both of them have just earned every cent of it.", {"human": True})
react("LEAD", "Phew! Let's all take a breath after that one.")
# Named praise
react("ANALYST", "What composure from {a}. Never rattled, not once, with {b} all over him.", {"named": True, "defending": True})
react("ANALYST", "That's patience from {a}. He waited, he waited, and then he struck at exactly the right moment.", {"named": True, "pass": True})
react("ANALYST", "Bravery from {a} on that move. You have to commit to go up the inside there, and he committed.", {"named": True, "pass": True, "lunge": True})
react("ANALYST", "Textbook defending from {a}. Every line covered, every door shut.", {"named": True, "defending": True})
react("LEAD", "Hats off to {b} as well. He never stopped coming, and he never put a wheel wrong.", {"named": True, "loser_human": True})
react("ANALYST", "{a} and {b}, two of the cleanest racers in this league. Close all the way and not a hint of a cheap move.", {"hh": True})
react("LEAD", "Real fighting spirit from {b}. He's lost the place, but he's won a lot of fans with that.", {"named": True, "pass": True, "loser_human": True})
react("ANALYST", "That's racecraft from {a}. Picking his spot, and making it stick.", {"named": True, "pass": True})
# Funny
react("ANALYST", "I think I need a lie down after that. And I was only watching.", register="humor")
react("LEAD", "Somebody check on the pit crews. I think they've chewed through their headsets.", register="humor")
react("ANALYST", "Those two have spent so long together tonight, they should probably split the fuel bill.", register="humor")
react("LEAD", "My heart rate's higher than the engine revs right now.", register="humor")
react("ANALYST", "If {b} gets any closer to that gearbox, he'll need to start paying rent.", {"hh": True, "defending": True}, register="humor")
react("LEAD", "I've seen less drama at a family wedding. And I've been to some family weddings.", register="humor")
react("ANALYST", "Can we get a replay? Actually, can we get every replay?", register="humor")
react("LEAD", "Someone tell the race director to put the kettle on. Everyone needs a cup of tea after that.", register="humor")
react("ANALYST", "That wasn't a battle, that was a full season of television in two laps.", register="humor")
react("LEAD", "And the charity wins again. Honestly, they should just let these two fight every lap.", {"human": True}, register="humor")
react("ANALYST", "Somewhere in the garage, a race engineer has just stopped pacing. First time in two laps.", register="humor")
react("LEAD", "I'm not saying that was close, but I think they swapped paint samples.", register="humor")
react("LEAD", "Kenny, you've gone very quiet.", register="humor",
      then={"speaker": "ANALYST", "text": "I've been holding my breath since turn five, Jon."})
react("ANALYST", "Those two have been glued together for so long, the stewards are checking if they're one car.", register="humor")
react("LEAD", "If you blinked during that, I'm sorry. You missed about forty overtakes.", register="humor")
react("ANALYST", "I'd ask {a} how he's feeling, but I think he's still trying to unclench his hands.", {"named": True, "winner_human": True}, register="humor")
react("LEAD", "That was so close, the timing screen needed a nap.", register="humor")
react("ANALYST", "Rumour has it they're still side by side on a parallel lap somewhere.", register="humor")
react("LEAD", "My notes for that battle just say wow, underlined three times.", register="humor")
react("ANALYST", "Right. Who's going to tell them it's only a charity race? Because I'm not.", {"human": True}, register="humor")

ADD["S_BAT_01"] = PHASES + R
ADD["S_LEAD_02"] = [V(v["speaker"], v["present"].replace("for {pos}", "for the lead"), v.get("when"),
                      **{k: v[k] for k in v if k not in ("speaker", "present", "when")})
                    for v in PHASES] + R

# ---- BAT-02 Defence -----------------------------------------------------------
ADD["S_BAT_02"] = [
    V("ANALYST", "Look at the job {a} is doing in front. {b} has been inside a second for a while now and he has not had a sniff.",
      {"beat": "holding"}),
    V("ANALYST", "{a} is under siege for {pos} from {b}, and so far every answer has been the right one.", {"beat": "holding"}),
    V("ANALYST", "{a} defending {pos}: covering the inside, braking late, giving {b} nothing to work with.", {"beat": "holding"}),
    V("ANALYST", "The pressure on {a} from {b} is relentless, {gap} back, and he is holding it together.", {"beat": "holding"}),
    V("ANALYST", "{a} is still holding {b} off for {pos}. That is lap after lap of pressure absorbed.", {"beat": "still_holding"}),
    V("ANALYST", "Still {a} ahead of {b}. The attack has not let up and neither has the defence.", {"beat": "still_holding"}),
    V("ANALYST", "{a} has held it. {b} gave it everything and {a} kept {pos}.", {"beat": "held"}),
    V("LEAD", "That is a defence won. {a} keeps {pos} and {b} has to think again.", {"beat": "held"}),
]

# ---- BAT-04 Side by side ------------------------------------------------------
ADD["S_BAT_04"] = [
    V("LEAD", "Side by side! {a} and {b} for {pos}!", {"beat": "alongside"}),
    V("LEAD", "They are wheel to wheel! {a} alongside {b}!", {"beat": "alongside"}),
    V("LEAD", "Two of ours side by side for {pos}, {a} and {b}, nobody lifting!", {"beat": "alongside", "hh": True}),
    V("LEAD", "{a} is alongside {b} for the lead!", {"beat": "alongside", "lead": True}),
    V("LEAD", "{a} makes it stick! Through on {b} for {pos}.", {"beat": "resolved", "outcome": "passed"}),
    V("LEAD", "And {a} wins that one, past {b} and into {pos}.", {"beat": "resolved", "outcome": "passed"}),
    V("ANALYST", "{a} holds the place. {b} was alongside and could not complete it.", {"beat": "resolved", "outcome": "held"}),
    V("ANALYST", "{a} keeps {pos}; {b} had the run but not the corner.", {"beat": "resolved", "outcome": "held"}),
]

# ---- BAT-05 Multi-car ---------------------------------------------------------
ADD["S_BAT_05"] = [
    V("LEAD", "A train is forming behind {a}: {count} cars nose to tail for {pos}, and {b} is in it.", {"beat": "train"}),
    V("LEAD", "{count} cars in one bunch now, {a} at the front of it, {b} one of ours in there.", {"beat": "train"}),
    V("ANALYST", "{count} cars covered by {span} for {pos}. One mistake and the whole thing changes.", {"beat": "train"}),
    V("ANALYST", "That train is growing: {count} cars now behind {a}.", {"beat": "grows"}),
    V("ANALYST", "Still {count} cars in the queue behind {a}, and {b} is still waiting his turn.", {"beat": "train_status"}),
    V("ANALYST", "The train behind {a} has broken up. {b} comes out of it {pos}.", {"beat": "thins"}),
    V("ANALYST", "That bunch of {count} has strung out now; {a} is {pos} out of it.", {"beat": "thins"}),
]

# ---- BAT-06 Stalemate ---------------------------------------------------------
ADD["S_BAT_06"] = [
    V("ANALYST", "{a} and {b}, {gap} apart for {pos}, and it has not moved for a while. Neither can do anything about the other.",
      {"beat": "stalemate"}),
    V("ANALYST", "This one has settled into a stalemate: {b} ahead, {a} {gap} back, nobody gaining.", {"beat": "stalemate"}),
    V("ANALYST", "{a} cannot get closer to {b}, and {b} cannot get away. {gap}, lap after lap.", {"beat": "stalemate"}),
]


def main():
    with open(PATH, "rb") as fh:
        doc = json.loads(fh.read().decode("utf-8"), object_pairs_hook=collections.OrderedDict)
    kinds = doc["kinds"]
    added = 0
    for kind, variants in ADD.items():
        entry = kinds.setdefault(kind, collections.OrderedDict([("variants", [])]))
        have = {v.get("present") for v in entry["variants"]}
        for v in variants:
            if v["present"] in have:
                continue
            entry["variants"].append(v)
            have.add(v["present"])
            added += 1
    doc["words_version"] = "V4-21OCT26"
    with open(PATH, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("added %d variants to %s" % (added, os.path.basename(PATH)))


if __name__ == "__main__":
    main()
