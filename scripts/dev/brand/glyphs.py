import json
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.boundsPen import BoundsPen

base = TTFont("fonts/BodoniModa.ttf")
inst = instantiateVariableFont(base, {"opsz": 96, "wght": 500})
gs = inst.getGlyphSet()
cmap = inst.getBestCmap()
upm = inst["head"].unitsPerEm
feats = (
    sorted({fr.FeatureTag for fr in inst["GSUB"].table.FeatureList.FeatureRecord})
    if "GSUB" in inst
    else []
)
print("features:", feats)

# small-caps mapping via GSUB single substitutions under 'smcp'
smcp = {}
if "smcp" in feats:
    gsub = inst["GSUB"].table
    idx = [
        i
        for i, fr in enumerate(gsub.FeatureList.FeatureRecord)
        if fr.FeatureTag == "smcp"
    ]
    for i in idx:
        for li in gsub.FeatureList.FeatureRecord[i].Feature.LookupListIndex:
            lk = gsub.LookupList.Lookup[li]
            for st in lk.SubTable:
                if hasattr(st, "mapping"):
                    smcp.update(st.mapping)
                elif getattr(st, "ExtSubTable", None) is not None and hasattr(
                    st.ExtSubTable, "mapping"
                ):
                    smcp.update(st.ExtSubTable.mapping)
print("smcp entries:", len(smcp))


def outline(gname, scale=1.0):
    g = gs[gname]
    pen = SVGPathPen(gs)
    # flip y (font y-up -> svg y-down), scale to 1 unit per em*scale
    tp = TransformPen(pen, (scale / upm, 0, 0, -scale / upm, 0, 0))
    g.draw(tp)
    bp = BoundsPen(gs)
    g.draw(bp)
    return {
        "d": pen.getCommands(),
        "adv": g.width * scale / upm,
        "bounds": [b * scale / upm for b in bp.bounds] if bp.bounds else None,
    }


out = {"upm": upm, "capHeight": inst["OS/2"].sCapHeight / upm, "glyphs": {}}
for ch in "UNITARES":
    gn = cmap[ord(ch)]
    out["glyphs"][ch] = outline(gn)
    if gn in smcp:
        out["glyphs"][ch.lower()] = outline(smcp[gn])
# kerning pairs (GPOS pair adjustment, flat) for the cap string
kern = {}
if "GPOS" in inst:
    gpos = inst["GPOS"].table
    kidx = [
        i
        for i, fr in enumerate(gpos.FeatureList.FeatureRecord)
        if fr.FeatureTag == "kern"
    ]
    lookups = set()
    for i in kidx:
        lookups.update(gpos.FeatureList.FeatureRecord[i].Feature.LookupListIndex)
    names = {cmap[ord(c)]: c for c in "UNITARES"}
    for gn in list(names):
        pass
    for li in lookups:
        lk = gpos.LookupList.Lookup[li]
        for st in lk.SubTable:
            if getattr(st, "ExtSubTable", None) is not None:
                st = st.ExtSubTable
            if st.LookupType != 2:
                continue
            if st.Format == 1:
                cov = st.Coverage.glyphs
                for ci, ps in enumerate(st.PairSet):
                    g1 = cov[ci]
                    for pvr in ps.PairValueRecord:
                        g2 = pvr.SecondGlyph
                        if (
                            g1 in names
                            and g2 in names
                            and pvr.Value1
                            and pvr.Value1.XAdvance
                        ):
                            kern[names[g1] + names[g2]] = pvr.Value1.XAdvance / upm
            elif st.Format == 2:
                cd1, cd2 = st.ClassDef1.classDefs, st.ClassDef2.classDefs
                cov = set(st.Coverage.glyphs)
                for g1, c1 in names.items():
                    if g1 not in cov:
                        continue
                    k1 = cd1.get(g1, 0)
                    for g2, c2 in names.items():
                        k2 = cd2.get(g2, 0)
                        rec = st.Class1Record[k1].Class2Record[k2]
                        if rec.Value1 and rec.Value1.XAdvance:
                            kern.setdefault(c1 + c2, rec.Value1.XAdvance / upm)
out["kern"] = kern
json.dump(out, open("glyphs.json", "w"))
print(
    "kern pairs for word:",
    {k: v for k, v in kern.items() if k in ["UN", "NI", "IT", "TA", "AR", "RE", "ES"]},
)
print("advances:", {c: round(v["adv"], 3) for c, v in out["glyphs"].items()})
print("U bounds:", out["glyphs"]["U"]["bounds"])
