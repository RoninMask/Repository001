#!/usr/bin/env python3
"""Build hoover_words_v4.json = hoover_words_v3.json + the V4 story kinds.
Run from the repo root. Deterministic."""
import json, collections

base = json.load(open("hoover_words_v3.json", encoding="utf-8"),
                 object_pairs_hook=collections.OrderedDict)

L, A = "LEAD", "ANALYST"
def v(speaker, present, past=None, **when):
    d = collections.OrderedDict()
    d["speaker"] = speaker
    if when:
        d["when"] = collections.OrderedDict(sorted(when.items()))
    d["present"] = present
    if past:
        d["past"] = past
    return d

K = collections.OrderedDict()

K["S_LEAD_01"] = [
    v(L, "{a} leads.", beat="open"),
    v(A, "{a} out in front, and the gap to {b} is {gap}.", beat="status"),
    v(L, "{a} still leads, {gap} clear of {b}.", beat="status"),
    v(A, "Up front, {a} has {gap} on {b}.", beat="status"),
    v(L, "{a} is getting away. The gap back to {b} is {gap}.", beat="breakaway"),
    v(A, "That is a breakaway now. {a} has {gap} in hand over {b}.", beat="breakaway"),
    v(L, "{b} is within {gap} of {a}. The lead is under threat.", beat="under_threat"),
    v(A, "The lead is not safe. {b} has closed to {gap} on {a}.", beat="under_threat"),
    v(A, "Settled at the front for now. {a} ahead of {b} by {gap}.", beat="procession"),
]

K["S_LEAD_02"] = [
    v(L, "{a} is on the leader, {gap} to {b} and closing.", beat="open"),
    v(L, "{a} is all over the back of {b}. The lead is on.", beat="open"),
    v(A, "A fight for the lead. {a} has {b} within {gap}.", beat="open"),
    v(L, "{a} is right with {b} now, {gap} back. This is for the lead.", beat="attack_range"),
    v(L, "{a} is right on the back of {b}. Nose to tail for the lead.", beat="attack_range"),
    v(A, "Now it is {a} behind {b}, {c} has dropped away.", beat="new_chaser"),
    v(A, "A new challenger. {a} is the car behind {b} now.", beat="new_chaser"),
    v(L, "Attack range for {a}. {b} has {gap} to defend.", beat="attack_range"),
    v(A, "DRS open for {a}. {b} will know it.", beat="drs"),
    v(L, "{a} has DRS on the leader.", beat="drs"),
    v(A, "{a} has dropped back to {gap}. {b} breathes again, for now.", beat="cooling"),
    v(L, "{a} takes the lead from {b}.", "{a} took the lead from {b}.", beat="resolved", outcome="passed"),
    v(A, "{b} holds on. {a} could not find a way through.", "{b} held on against {a}.", beat="resolved", outcome="held"),
    v(A, "That fight for the lead is over. {b} stays in front of {a}.", beat="resolved", outcome="superseded"),
    v(A, "Still {gap} between {a} and {b} at the front.", beat="status"),
    v(L, "{a} is still there, {gap} behind {b}.", beat="status"),
]

K["S_LEAD_03"] = [
    v(L, "{a} leads, and {b} is second.", "{a} took the lead from {b}.", beat="changed", pit=False),
    v(L, "New leader. {a} is in front of {b}.", "{a} went into the lead ahead of {b}.", beat="changed", pit=False),
    v(A, "{a} inherits the lead through the stops. {b} is behind now.", beat="changed", pit=True),
]

K["S_BAT_01"] = [
    v(L, "{a} is closing on {b} for {pos}, {gap} back.", beat="open"),
    v(L, "{a} is right with {b} for {pos}.", beat="open"),
    v(A, "Watch {a}. He is catching {b}, and the gap is {gap}.", beat="open"),
    v(A, "{a} is coming for {b}. At this rate he gets there.", beat="open", projects=True),
    v(L, "{a} is catching {b} hand over fist, {gap} now.", beat="big_catch"),
    v(A, "That is a big catch. {a} on {b}, {gap} and falling fast.", beat="big_catch"),
    v(L, "{a} is on the back of {b} for {pos}, {gap} back.", beat="attack_range"),
    v(L, "{a} is right on {b} for {pos}. Nose to tail.", beat="attack_range"),
    v(L, "Here it comes. {a} within {gap} of {b}.", beat="attack_range"),
    v(A, "{a} is in range of {b}. {pos} is on the line.", beat="attack_range"),
    v(L, "DRS for {a} on {b}.", beat="drs"),
    v(A, "{a} has the wing open on {b}.", beat="drs"),
    v(A, "{a} has dropped off {b}, {gap} now.", beat="cooling"),
    v(L, "{a} goes through on {b} for {pos}.", "{a} went through on {b} for {pos}.", beat="resolved", outcome="passed"),
    v(L, "{a} takes {pos} from {b}.", "{a} took {pos} from {b}.", beat="resolved", outcome="passed"),
    v(A, "{b} holds {a} off. That one is done.", "{b} held {a} off.", beat="resolved", outcome="held"),
    v(A, "{a} could not make it stick. {b} keeps the place.", beat="resolved", outcome="failed"),
    v(A, "That battle has gone cold. {b} has pulled clear of {a}.", beat="resolved", outcome="separated"),
    v(A, "{a} and {b}, the fight for {pos}, has broken up.", beat="resolved", outcome="lost"),
    v(A, "{a} still {gap} behind {b}.", beat="status"),
    v(L, "No change between {a} and {b}, {gap} between them.", beat="status"),
]

K["S_BAT_03"] = [
    v(L, "{a} and {b} are swapping that place. {a} has it for now.", beat="open"),
    v(A, "Back and forth between {a} and {b}. This is one contested position.", beat="open"),
    v(L, "That is {swaps} times they have traded it. {a} ahead again.", beat="third_swap"),
    v(A, "{a} settles it ahead of {b} after {swaps} swaps.", "{a} settled it ahead of {b}.", beat="settled", swaps_one=False),
    v(A, "{a} settles it ahead of {b} after {swaps} swap.", "{a} settled it ahead of {b}.", beat="settled", swaps_one=True),
    v(L, "{a} comes out of that in front of {b}.", "{a} came out of that in front of {b}.", beat="settled"),
]

K["S_POS_01"] = [
    v(L, "{a} is through on {b} for {pos}.", "{a} went through on {b} for {pos}.", beat="earned"),
    v(L, "{a} takes {pos}.", "{a} took {pos}.", beat="earned"),
    v(A, "{a} moves up to {pos}, and that one was gifted. A car ahead has gone.", beat="gifted"),
    v(A, "{a} inherits {pos}.", beat="gifted"),
    v(A, "{a} loses a place to {b}, down to {pos}.", "{a} lost a place to {b}.", beat="lost"),
    v(L, "{b} is past {a}, and {a} drops to {pos}.", beat="lost"),
]

K["S_POS_03"] = [
    v(L, "{a} has lost {places} {cause}.", "{a} lost {places} {cause}.", beat="open", cause_known=True),
    v(A, "{a} is going backwards, {places}, {cause}.", beat="open", cause_known=True),
    v(L, "{a} has dropped {places}. No obvious reason yet.", "{a} dropped {places}.", beat="open", cause_known=False),
    v(A, "Something has happened to {a}. {places} gone, and we do not know why yet.", beat="open", cause_known=False),
    v(A, "That is {places} now for {a}. A collapse.", beat="magnitude"),
    v(A, "{a} has stopped the bleeding, {places} lost, now in {pos}.", beat="arrested"),
    v(L, "{a} steadies it in {pos}.", beat="arrested"),
]

K["S_POS_05"] = [
    v(L, "{a} is into the podium places, {pos}.", beat="podium", direction="up"),
    v(A, "{a} is out of the podium places, down to {pos}.", beat="top5", direction="down"),
    v(L, "{a} is in the top five, {pos}.", beat="top5", direction="up"),
    v(L, "{a} is into the points, {pos}.", beat="points", direction="up"),
    v(A, "{a} drops to {pos}. That is out of the top five.", beat="points", direction="down"),
    v(A, "{a} is out of the points, down in {pos}.", beat="outside", direction="down"),
    v(A, "{a} is back in the podium places, {pos}.", beat="podium", direction="up"),
    v(A, "{a} slips out of the podium places to {pos}.", beat="top5", direction="down"),
    v(A, "{a} is out of the podium places, {pos} now.", beat="points", direction="down"),
]

K["S_PACE_01"] = [
    v(L, "Fastest lap of the race for {a}, {time}.", beat="fastest"),
    v(A, "{a} sets the fastest lap, {time}.", beat="fastest"),
    v(L, "{a} goes quickest of anyone, {time}, and that beats the whole field.", beat="fastest", beats_ai=True),
]

K["S_STR_01"] = [
    v(A, "{a} is in the pits, the {count} stop, from {pos}.", beat="in"),
    v(A, "Box for {a}. He was running {pos}.", beat="in"),
    v(A, "{a} pits under the safety car. The cheap stop.", beat="in", under_sc=True),
    v(A, "{a} rejoins in {pos}, {places} down on where he was.", beat="rejoined", lost=True, action=False),
    v(A, "{a} is back out in {pos}, {places} lost in the stop, clear air ahead.", beat="rejoined", lost=True, action=False),
    v(A, "{a} rejoins in {pos}.", beat="rejoined", action=False),
    v(L, "{a} rejoins in {pos}, right with {b}. That is a fight.", beat="rejoined", into_human=True),
    v(L, "Out comes {a} in {pos}, and {b} is {gap} away. Game on.", beat="rejoined", into_human=True),
    v(L, "{a} comes out of the pits in {pos}, straight into {b}. {places} down, and a battle on his hands.", beat="rejoined", action=True, lost=True),
    v(L, "{a} rejoins in {pos}, {gap} behind {b}. He will want that place back.", beat="rejoined", action=True),
]

K["S_INC_01"] = [
    v(L, "Contact! {a} and {b}.", "Contact between {a} and {b}.", beat="contact"),
    v(L, "{a} and {b} have touched.", beat="contact"),
    v(L, "Contact between {a} and {b}! Two of our drivers together.", "Contact between {a} and {b}.", beat="contact", human_human=True),
    v(A, "{a} has lost {places} out of that, down to {pos}.", beat="consequence"),
    v(A, "The cost of that is {places} for {a}.", beat="consequence"),
]

K["S_INC_02"] = [
    v(L, "{a} is off!", "{a} went off.", beat="off"),
    v(L, "{a} has gone off the track.", "{a} went off the track.", beat="off"),
    v(A, "{a} rejoins, and that cost him {places}, {pos} now.", beat="rejoined", lost=True),
    v(A, "{a} is back on, and got away with it, still {pos}.", beat="rejoined", lost=False),
]

K["S_INC_05"] = [
    v(L, "{a} is out of the race, {cause}.", "{a} was out of the race, {cause}.", beat="out"),
    v(A, "That is the end of the race for {a}, {cause}, and he was running {pos}.", beat="out"),
]

K["S_INC_06"] = [
    v(L, "Trouble at the start, {count} cars involved.", "There was trouble at the start.", beat="chaos"),
    v(A, "A first lap incident with {count} cars in it. Let us sort out the order.", beat="chaos"),
]

K["S_RC_02"] = [
    v(A, "That is {cause}, and it resets every gap in the field.", beat="deployed", cause_known=True),
    v(A, "That resets every gap in the field.", beat="deployed"),
    v(A, "Every gap in the field is gone. They will all bunch up.", beat="deployed", vsc=False),
    v(A, "{laps} laps behind the safety car, and now they race.", beat="restart", vsc=False),
    v(A, "The field spent {laps} laps behind the safety car.", beat="restart", vsc=False),
]

K["S_RC_03"] = [
    v(A, "They will reform on the grid.", beat="waiting"),
    v(A, "A standing restart from the grid when they go again.", beat="waiting"),
]

K["S_RC_05"] = [
    v(L, "A {seconds} second penalty for {a}, {cause}.", "{a} was given a penalty.", beat="issued", cause_known=True),
    v(L, "Penalty for {a}, {seconds} seconds.", "{a} was given a penalty.", beat="issued"),
    v(A, "{a} has a penalty to carry, {cause}.", beat="issued", cause_known=True),
    v(A, "That is a penalty for {a}.", beat="issued"),
]

K["S_REL_02"] = [
    v(L, "{a} is out, {cause}.", "{a} dropped out.", beat="out"),
    v(A, "We have lost {a}, {cause}, from {pos}.", beat="out"),
    v(A, "{a} has dropped out of the session.", "{a} dropped out of the session.", beat="out", disconnected=True),
]

K["S_SF_01"] = [
    v(L, "{a} starts from {grid}.", beat="on_grid", hook=False),
    v(A, "{a} lines up {grid} on the grid.", beat="on_grid", hook=False),
    # 09 OCT: the grid intro carries the driver's interview headline
    v(L, "{a} starts from {grid}. {hook}", beat="on_grid", hook=True),
    v(A, "{a} lines up {grid} on the grid. {hook}", beat="on_grid", hook=True),
]

K["S_SF_02"] = [
    v(L, "Away on the formation lap.", beat="rolling"),
    v(A, "Formation lap. Tyres and brakes up to temperature, and then it is on.", beat="rolling"),
]

K["S_SF_03"] = [
    v(L, "Great start from {a}, up {places} to {pos}!", "{a} gained {places} at the start.", beat="good_start"),
    v(A, "{a} got off the line beautifully, {places} gained, {pos} now.", beat="good_start"),
    v(L, "Bad start for {a}, down {places} to {pos}.", "{a} lost {places} at the start.", beat="bad_start"),
    v(A, "{a} bogged down there, {places} lost, {pos} now.", beat="bad_start"),
]

K["S_SF_04"] = [
    v(A, "First lap done, and {a} is up {places} to {pos}.", beat="net", up=True),
    v(A, "After the opening lap, {a} is down {places} in {pos}.", beat="net", up=False),
    v(L, "{a} is {pos} after lap one.", beat="net"),
]

K["S_SF_05"] = [
    v(A, "Quarter distance, {remaining} laps to go.", beat="fraction", key="frac_25"),
    v(A, "Half distance, {remaining} laps left.", beat="fraction", key="frac_50"),
    v(A, "Three quarters done, {remaining} to go.", beat="fraction", key="frac_75"),
    v(A, "Lap {laps}, {remaining} to go.", beat="fraction"),
    v(L, "{remaining} laps to go.", beat="to_go", one=False),
    v(L, "One lap to go.", beat="to_go", one=True),
    v(A, "{remaining} remaining.", beat="to_go", one=False),
]

K["S_SF_06"] = [
    v(L, "Final lap, and {a} is {gap} behind {b}!", beat="final_lap", battle=True),
    v(L, "Last lap, {a} on {b}, {gap} between them.", beat="final_lap", battle=True),
    v(L, "Final lap.", beat="final_lap"),
    v(A, "One lap left and the order looks set.", beat="final_lap", battle=False),
]

K["S_SF_07"] = [
    v(A, "Best of our drivers tonight is {a}, home in {pos}.", beat="best_human", only=False),
    v(L, "{a} takes the honours among our drivers, {pos} at the flag, ahead of {b}.", beat="best_human", only=False),
    v(L, "{a} is the top finisher of our drivers, in {pos}.", beat="best_human", only=False),
    v(L, "{a} brings it home in {pos}.", beat="best_human", only=True),
    v(A, "That is {a}'s night done, {pos} at the flag.", beat="best_human", only=True),
    # 09 OCT: the pre-race interview checked against the result
    v(A, "Before the race {a} told Sienna {said} was the target, and {said} is exactly where he finished.", beat="prediction", hit=True),
    v(A, "{a} predicted {said} before the race and has done better than that, home in {pos}.", beat="prediction", better=True),
    v(A, "{a} told Sienna he was aiming for {said}. He finished {pos}.", beat="prediction", worse=True),
    v(A, "{a} had talked about {said} before the race. It did not get that far tonight.", beat="prediction", retired=True),
    v(L, "And {a} got the one he wanted most: he finished ahead of {b}.", beat="rival", ahead=True),
    v(L, "{a} said {b} was the driver he most wanted to beat. {b} had the better of him tonight.", beat="rival", ahead=False),
]

K["S_HUM_01"] = [
    v(L, "{a} and {b} are together on the road. This is the race.", beat="open"),
    v(A, "{count} of our drivers within a few seconds of each other. {a} and {b} at the heart of it.", beat="open"),
    v(L, "Three of them together now! {a}, {b}, and company.", beat="open", three=True),
    v(L, "{a} joins the group. That is {count} of our drivers nose to tail.", beat="joins"),
    v(A, "{a} makes it {count}. Watch this.", beat="joins"),
    v(A, "The group thins to {count}.", beat="leaves"),
    v(A, "From {a} to {b}, {count} cars, covered by {gap}.", beat="status"),
    v(L, "Still {count} of them together, and {a} leads the group.", beat="status"),
    v(A, "That group has split up. {a} out on his own now.", beat="dispersed"),
]

K["S_HUM_05"] = [
    v(L, "{a} is now the lead runner among our drivers, ahead of {b}, {pos} overall.", "{a} took over as the lead runner among our drivers.", beat="lead_human"),
    v(A, "Change at the top of our group. {a} is past {b}.", beat="lead_human"),
]

K["S_HUM_06"] = [
    v(A, "{a} loses out to {b}, down to {pos}.", beat="picked_off"),
    v(A, "{b} has got past {a}. That hurts, {pos} now.", beat="picked_off"),
    v(L, "{a} clears {b}, up to {pos}.", "{a} cleared {b}.", beat="cleared"),
    v(A, "Good move from {a} on {b}, up to {pos}.", beat="cleared"),
]

K["S_HUM_07"] = [
    v(A, "Checking on {a}, running {pos}, {gap} behind {b}.", beat="check_in", has_ahead=True),
    v(A, "{a} is {pos}, with {b} {gap} up the road.", beat="check_in", has_ahead=True),
    v(A, "Let us not forget {a}, {pos} and running his own race.", beat="check_in"),
    v(L, "{a} in {pos}.", beat="check_in"),
]

K["S_HUM_08"] = [
    v(A, "{a} is running with limited data tonight, so we will see less of him than we would like.", beat="restricted"),
    v(A, "We have no data from {a} this evening.", beat="restricted"),
]

K["S_DEV_06"] = [
    v(A, "That is {count} cars out, {n} still running.", beat="count"),
    v(A, "The retirements are up to {count}. The field is down to {n}.", beat="count"),
]

K["S_DEV_08"] = [
    v(A, "That settles the podium, and the best {a} can do from {pos} is {ceiling}.", beat="podium_gone"),
    v(A, "The podium is out of reach for {a} now, and {ceiling} is his ceiling.", beat="podium_gone"),
]

K["S_RELATE"] = [
    # defend: a threat from behind
    v(A, "{a} has {b} right behind him for {pos}. That is a fight now.", beat="relate", ctype="defend", fight=True),
    v(A, "And that brings {b} onto the back of {a}, {gap} between them.", beat="relate", ctype="defend", fight=True),
    v(A, "{b} is coming for {a}. {gap} back and closing at {rate}; {a} has {laps} before that is a fight.", beat="relate", ctype="defend", reaches=True),
    v(A, "Watch {a}'s mirrors. {b} is {gap} behind and taking {rate} out of him, so about {laps} and he is there.", beat="relate", ctype="defend", reaches=True),
    v(A, "{b} is {places} behind {a} and closing, {gap} to make up. {a} has {laps} to answer that.", beat="relate", ctype="defend", reaches=True),
    # attack: an opportunity ahead
    v(A, "That is {gap} up the road from {a}, and he is closing. {b} is the next car he can reach.", beat="relate", ctype="attack", reaches=True),
    v(A, "{a} is taking {rate} out of {b}, {gap} ahead. At that rate he is on him in {laps}.", beat="relate", ctype="attack", reaches=True),
    v(A, "Good news for {a}: {b} is {gap} ahead and slowing. That is {laps} away.", beat="relate", ctype="attack", reaches=True),
    v(A, "{a} is right with {b} for {pos}. That is his chance.", beat="relate", ctype="attack", fight=True),
    v(A, "And {a} is on the back of that, {gap} off {b}.", beat="relate", ctype="attack", fight=True),
    # deal: the race changed for him
    v(A, "That hands {a} {places}. He is {pos} now.", beat="relate", ctype="deal", what="gained"),
    v(A, "{a} gains {places} out of that without passing anyone, up to {pos}.", beat="relate", ctype="deal", what="gained"),
    v(A, "That changes what {a} can reach tonight.", beat="relate", ctype="deal", what="projection"),
    v(A, "It moves the picture for {a}, running {pos}.", beat="relate", ctype="deal", what="projection"),
]

# ---- the lull programme (07 OCT): revisit / human race so far / stats ----
K["S_LULL_REVISIT"] = [
    v(A, "Still live, that one: {a} is {gap} behind {b} for {pos}, and closing.", beat="lull_revisit", trend="closing", reaches=False),
    v(A, "{a} is taking {rate} out of {b}. {gap} to close, and at that rate he is there in {laps} laps.", beat="lull_revisit", trend="closing", reaches=True),
    v(A, "Keep an eye on {a} and {b} for {pos}. {gap} between them, and {a} has the pace. {laps} laps and he is on him.", beat="lull_revisit", trend="closing", reaches=True),
    v(A, "{a} is closing on {b}, {gap} now, but with {remaining} laps left it may not be enough.", beat="lull_revisit", trend="closing", reaches=False),
    v(A, "{b} is holding {a} at {gap} for {pos}. Nothing in it either way.", beat="lull_revisit", trend="steady"),
    v(A, "That one has settled for now: {a} sits {gap} behind {b}, and the gap is not moving.", beat="lull_revisit", trend="steady"),
    v(A, "{b} is edging away from {a}, {gap} now and growing.", beat="lull_revisit", trend="opening"),
    v(A, "{a} has lost touch with {b} for the moment. {gap}, and {b} is the quicker car right now.", beat="lull_revisit", trend="opening"),
]

K["S_LULL_HUMAN"] = [
    v(L, "{a} runs {pos}, up {places} from {grid} on the grid, with {remaining} laps to go.", beat="lull_human", up=True),
    v(L, "Where is {a}? {pos}, from {grid} at the start, and {b} is {gap} up the road.", beat="lull_human", up=True, has_ahead=True),
    v(L, "{a} started {grid} and is running {pos} now, {gap} behind {b}, with {c} {delta} back.", beat="lull_human", has_ahead=True, has_behind=True),
    v(L, "{a} is {pos}, {places} down on where he started, and {b} is {gap} ahead.", beat="lull_human", down=True, has_ahead=True),
    v(L, "{a} is holding {pos}. {remaining} laps left and {b} is {gap} up the road.", beat="lull_human", has_ahead=True),
    v(L, "{a} is in {pos} with {remaining} laps to run, and {c} is {delta} behind him.", beat="lull_human", has_behind=True),
    v(L, "{a} leads the race with {remaining} laps to go.", beat="lull_human", has_ahead=False),
    v(L, "{a} runs {pos}, {remaining} laps to go.", beat="lull_human"),
]

K["S_LULL_STATS"] = [
    v(A, "{a} has led {n} laps of this race.", beat="lull_stats", stat="laps_led"),
    v(A, "Every lap so far has had {a} at the front: {n} of them.", beat="lull_stats", stat="laps_led"),
    v(A, "Fastest lap of the race still stands to {a}, a {time}.", beat="lull_stats", stat="fastest"),
    v(A, "The quickest lap we have seen tonight is {a}'s {time}.", beat="lull_stats", stat="fastest"),
    v(A, "One car out of this race so far.", beat="lull_stats", stat="out", one=True),
    v(A, "{n} cars out of this race so far.", beat="lull_stats", stat="out", one=False),
]

doc = collections.OrderedDict(base)
doc["words_version"] = "V4-07OCT26"
doc["_about_v4"] = ("V4: the V3 words file plus one kind per Story Matrix row in the "
                    "Pass 1 set (S_<ROW>) and S_RELATE. Variants are gated per beat with "
                    "when.beat and optional view keys. Placeholders come from the story "
                    "superset; select() skips variants the context cannot fill.")
kinds = collections.OrderedDict(base["kinds"])
for k, variants in K.items():
    kinds[k] = collections.OrderedDict([("variants", variants)])
doc["kinds"] = kinds
json.dump(doc, open("hoover_words_v4.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print("story kinds:", len(K), "variants:", sum(len(x) for x in K.values()))
