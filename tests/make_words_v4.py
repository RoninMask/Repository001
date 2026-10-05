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
    v(A, "A fight for the lead. {a} has {b} within {gap}.", beat="open"),
    v(L, "{a} is right with {b} now, {gap} back. This is for the lead.", beat="attack_range"),
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
    v(A, "Watch {a}. He is catching {b}, and the gap is {gap}.", beat="open"),
    v(A, "{a} is coming for {b}. At this rate he gets there.", beat="open", projects=True),
    v(L, "{a} is catching {b} hand over fist, {gap} now.", beat="big_catch"),
    v(A, "That is a big catch. {a} on {b}, {gap} and falling fast.", beat="big_catch"),
    v(L, "{a} is on the back of {b} for {pos}, {gap} back.", beat="attack_range"),
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
    v(L, "{a} has lost {places} places {cause}.", "{a} lost {places} places {cause}.", beat="open", cause_known=True),
    v(A, "{a} is going backwards, {places} places, {cause}.", beat="open", cause_known=True),
    v(L, "{a} has dropped {places} places. No obvious reason yet.", "{a} dropped {places} places.", beat="open", cause_known=False),
    v(A, "Something has happened to {a}. {places} places gone, and we do not know why yet.", beat="open", cause_known=False),
    v(A, "That is {places} places now for {a}. A collapse.", beat="magnitude"),
    v(A, "{a} has stopped the bleeding, {places} places lost, now in {pos}.", beat="arrested"),
    v(L, "{a} steadies it in {pos}.", beat="arrested"),
]

K["S_POS_05"] = [
    v(L, "{a} is into the podium places, {pos}.", beat="podium", direction="up"),
    v(A, "{a} is out of the podium places, down to {pos}.", beat="top5", direction="down"),
    v(L, "{a} is in the top five, {pos}.", beat="top5", direction="up"),
    v(L, "{a} is into the points, {pos}.", beat="points", direction="up"),
    v(A, "{a} drops to {pos}. That is out of the top five.", beat="points", direction="down"),
    v(A, "{a} is out of the points, down in {pos}.", beat="outside", direction="down"),
    v(A, "{a} has moved to {pos}.", beat="podium"),
    v(A, "{a} has moved to {pos}.", beat="top5"),
    v(A, "{a} has moved to {pos}.", beat="points"),
    v(A, "{a} has moved to {pos}.", beat="outside"),
]

K["S_PACE_01"] = [
    v(L, "Fastest lap of the race for {a}, {time}.", beat="fastest"),
    v(A, "{a} sets the fastest lap, {time}.", beat="fastest"),
    v(L, "{a} goes quickest of anyone, {time}, and that beats the whole field.", beat="fastest", beats_ai=True),
]

K["S_STR_01"] = [
    v(L, "{a} is in the pits, the {count} stop, from {pos}.", beat="in"),
    v(A, "Box for {a}. He was running {pos}.", beat="in"),
    v(A, "{a} pits under the safety car. The cheap stop.", beat="in", under_sc=True),
    v(L, "{a} rejoins in {pos}.", beat="rejoined"),
    v(A, "{a} comes back out in {pos}.", beat="rejoined"),
    v(L, "{a} rejoins in {pos}, right with {b}. That is a fight.", beat="rejoined", into_human=True),
    v(A, "Out comes {a} in {pos}, and {b} is {gap} away. Game on.", beat="rejoined", into_human=True),
]

K["S_INC_01"] = [
    v(L, "Contact! {a} and {b}.", "Contact between {a} and {b}.", beat="contact"),
    v(L, "{a} and {b} have touched.", beat="contact"),
    v(L, "Contact between {a} and {b}! Two of our drivers together.", "Contact between {a} and {b}.", beat="contact", human_human=True),
    v(A, "{a} has lost {places} places out of that, down to {pos}.", beat="consequence"),
    v(A, "The cost of that is {places} places for {a}.", beat="consequence"),
]

K["S_INC_02"] = [
    v(L, "{a} is off!", "{a} went off.", beat="off"),
    v(L, "{a} has gone off the track.", "{a} went off the track.", beat="off"),
    v(A, "{a} rejoins, and that cost him {places} places, {pos} now.", beat="rejoined", lost=True),
    v(A, "{a} is back on, and got away with it, still {pos}.", beat="rejoined", lost=False),
]

K["S_INC_05"] = [
    v(L, "{a} is out of the race, {cause}.", "{a} was out of the race, {cause}.", beat="out"),
    v(A, "That is the end of the race for {a}, {cause}, and he was running {pos}.", beat="out"),
]

K["S_INC_06"] = [
    v(L, "Trouble at the start. {count} cars involved.", "There was trouble at the start.", beat="chaos"),
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
    v(L, "{a} starts from {grid}.", beat="on_grid"),
    v(A, "{a} lines up {grid} on the grid.", beat="on_grid"),
]

K["S_SF_02"] = [
    v(L, "Away on the formation lap.", beat="rolling"),
    v(A, "Formation lap. Tyres and brakes up to temperature, and then it is on.", beat="rolling"),
]

K["S_SF_03"] = [
    v(L, "Great start from {a}, up {places} places to {pos}!", "{a} gained {places} places at the start.", beat="good_start"),
    v(A, "{a} got off the line beautifully, {places} places gained, {pos} now.", beat="good_start"),
    v(L, "Bad start for {a}, down {places} to {pos}.", "{a} lost {places} places at the start.", beat="bad_start"),
    v(A, "{a} bogged down there, {places} places lost, {pos} now.", beat="bad_start"),
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
    v(A, "Best of our drivers tonight is {a}, home in {pos}.", beat="best_human"),
    v(L, "{a} takes the honours among our drivers, {pos} at the flag, ahead of {b}.", beat="best_human", only=False),
    v(L, "{a} is the top finisher of our drivers, in {pos}.", beat="best_human"),
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
    v(A, "All of that is {gap} up the road from {a}.", beat="relate", ahead=False),
    v(A, "And {a} is {gap} back from that, in his own race.", beat="relate", ahead=False),
    v(A, "{a} is {places} places behind that.", beat="relate", ahead=False),
    v(A, "That is {places} places ahead of {a}.", beat="relate", ahead=False, input="places"),
    v(A, "{a} is {places} places ahead of all that, and {gap} up the road.", beat="relate", ahead=True),
    v(A, "That is behind {a}, by {gap}.", beat="relate", ahead=True),
    v(A, "{a} is {places} places ahead of all that.", beat="relate", ahead=True),
    v(A, "All of that is {places} places behind {a}.", beat="relate", ahead=True),
    v(A, "{a} is {places} places back from that, and it changes what he can still reach tonight.", beat="relate", input="ceiling", ahead=False),
    v(A, "{a} is {gap} from that, close enough to be part of it.", beat="relate", input="chance", ahead=False),
]

doc = collections.OrderedDict(base)
doc["words_version"] = "V4-05OCT26"
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
