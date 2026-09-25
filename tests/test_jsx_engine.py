"""Run the generated Photoshop script against the fake Photoshop in tests/jsx."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ps_remover import api, shapes
from ps_remover.script import build_script

NODE = shutil.which("node")
HARNESS = Path(__file__).parent / "jsx" / "mock_photoshop.js"

PHOTO = "/photos/고양이.jpg"
OUTPUT = "/photos/고양이_removed.jpg"


def remove_config(shape_list=None, options=None, image_size=None, **overrides):
    config = api.build_remove_config(
        "photo.jpg", "out.jpg",
        [shapes.rect(100, 50, 300, 250)] if shape_list is None else shape_list,
        options or api.RemoveOptions(), image_size,
    )
    config.update(input=PHOTO, output=OUTPUT)
    config.update(overrides)
    return config


@unittest.skipUnless(NODE, "node is not installed")
class JsxEngineTests(unittest.TestCase):
    def run_script(self, config, **scenario):
        scenario.setdefault("files", [PHOTO])
        scenario.setdefault("images", {PHOTO: {"width": 4000, "height": 3000}})
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "job.jsx"
            script_path.write_text(build_script(config), encoding="ascii")
            scenario_path = Path(tmp) / "scenario.json"
            scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
            proc = subprocess.run(
                [NODE, str(HARNESS), str(script_path), str(scenario_path)],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertIsNone(out["error"], out["error"])
        if out["es3Error"] != "not checked":
            self.assertIsNone(out["es3Error"], "script is not valid ECMAScript 3")
        out["report"] = json.loads(out["result"])
        # Preferences are always restored.
        self.assertEqual(out["preferences"], {"rulerUnits": "Units.CM", "displayDialogs": "DialogModes.ALL"})
        return out

    @staticmethod
    def calls(out, name):
        return [entry for entry in out["log"] if entry[0] == name]

    @staticmethod
    def actions(out, event=None):
        return [entry for entry in out["log"] if entry[0] == "executeAction" and (event is None or entry[1] == event)]

    # ------------------------------------------------------------ remove

    def test_basic_content_aware_removal(self):
        out = self.run_script(remove_config())
        report = out["report"]
        self.assertTrue(report["ok"])
        self.assertEqual(report["output"], OUTPUT)
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["selectionBounds"], [96, 46, 304, 254])  # grown by the default 4 px
        self.assertEqual(report["document"]["width"], 4000)

        select = self.actions(out, "setd")[0]
        target = select[2]["T   "]
        self.assertEqual(select[2]["null"], {"ref": [{"cls": "Chnl", "property": "fsel"}]})
        self.assertEqual(target["cls"], "Rctn")
        self.assertEqual({k: v["value"] for k, v in target["desc"].items()},
                         {"Top ": 50, "Left": 100, "Btom": 250, "Rght": 300})
        self.assertTrue(all(v["unit"] == "#Pxl" for v in target["desc"].values()))
        self.assertEqual(select[3], "DialogModes.NO")

        fill = self.actions(out, "Fl  ")
        self.assertEqual(len(fill), 1)
        self.assertEqual(fill[0][2]["Usng"], {"enumType": "FlCn", "value": "contentAware"})
        self.assertTrue(fill[0][2]["contentAwareColorAdaptationFill"])

        save = self.calls(out, "saveAs")
        self.assertEqual(len(save), 1)
        _, _, path, options, as_copy, *_ = save[0]
        self.assertEqual(path, OUTPUT)
        self.assertEqual(options["kind"], "jpeg")
        self.assertEqual(options["quality"], 12)
        self.assertTrue(as_copy)

        # The working document is closed and the saved result opened instead.
        names = [entry[0] for entry in out["log"]]
        self.assertLess(names.index("saveAs"), names.index("close"))
        self.assertEqual([d["file"] for d in out["documents"]], [OUTPUT])
        self.assertTrue(report["openedOutput"])
        self.assertIn("bringToFront", names)
        self.assertEqual(out["alerts"], [])

    def test_high_resolution_photo_is_edited_at_72_ppi(self):
        out = self.run_script(remove_config(), images={PHOTO: {"width": 4000, "height": 3000, "resolution": 300}})
        resize = self.calls(out, "resizeImage")
        self.assertEqual([entry[4] for entry in resize], [72, 300])
        self.assertEqual({entry[5] for entry in resize}, {"ResampleMethod.NONE"})
        self.assertEqual(self.calls(out, "expand")[0][3], 72)
        # Saved with the original resolution.
        self.assertEqual(self.calls(out, "saveAs")[0][5], 300)

    def test_72_ppi_photo_is_not_resized(self):
        out = self.run_script(remove_config())
        self.assertEqual(self.calls(out, "resizeImage"), [])

    def test_shapes_add_and_subtract_in_order(self):
        shape_list = [
            shapes.rect(0, 0, 50, 50, mode="subtract"),  # nothing selected yet: skipped
            shapes.ellipse(10, 10, 110, 60),
            shapes.polygon([(200, 200), (300, 200), (250, 280)]),
            shapes.rect(20, 20, 40, 40, mode="subtract"),
        ]
        out = self.run_script(remove_config(shape_list))
        events = [(entry[1], entry[2]["T   "]["cls"]) for entry in self.actions(out) if entry[1] in ("setd", "AddT", "SbtF")]
        self.assertEqual(events, [("setd", "Elps"), ("AddT", "Plgn"), ("SbtF", "Rctn")])
        ellipse, polygon = self.actions(out, "setd")[0][2], self.actions(out, "AddT")[0][2]
        self.assertTrue(ellipse["AntA"])
        points = [(p["desc"]["Hrzn"]["value"], p["desc"]["Vrtc"]["value"]) for p in polygon["T   "]["desc"]["Pts "]]
        self.assertEqual(points, [(200, 200), (300, 200), (250, 280)])
        self.assertEqual(polygon["T   "]["desc"]["Pts "][0]["cls"], "Pnt ")

    def test_expand_and_feather(self):
        out = self.run_script(remove_config(options=api.RemoveOptions(expand=0, feather=2.5)))
        self.assertEqual(self.calls(out, "expand"), [])
        self.assertEqual(self.calls(out, "feather")[0][1:3], [2.5, "px"])

    def test_empty_selection_is_an_error(self):
        out = self.run_script(remove_config([shapes.rect(5000, 5000, 5100, 5100)]))
        report = out["report"]
        self.assertFalse(report["ok"])
        self.assertIn("선택 영역이 비어", report["error"])
        self.assertEqual(self.actions(out, "Fl  "), [])
        self.assertTrue(report["leftOpen"])  # left open for manual work
        self.assertEqual([d["file"] for d in out["documents"]], [PHOTO])

    def test_failure_closes_photo_when_not_keeping_it_open(self):
        out = self.run_script(remove_config(options=api.RemoveOptions(keep_open=False)), failEvents=["Fl  "])
        report = out["report"]
        self.assertFalse(report["ok"])
        self.assertIn("not currently available", report["error"])
        self.assertNotIn("leftOpen", report)
        self.assertEqual(out["documents"], [])

    def test_resolution_restored_after_failure(self):
        out = self.run_script(remove_config(), failEvents=["Fl  "],
                              images={PHOTO: {"width": 4000, "height": 3000, "resolution": 240}})
        self.assertFalse(out["report"]["ok"])
        self.assertEqual(out["documents"][0]["resolution"], 240)

    def test_missing_input(self):
        out = self.run_script(remove_config(), files=[])
        self.assertFalse(out["report"]["ok"])
        self.assertIn("찾을 수 없습니다", out["report"]["error"])

    def test_already_open_photo_is_duplicated(self):
        out = self.run_script(remove_config(), openDocuments=[{"file": PHOTO, "width": 4000, "height": 3000, "saved": False}])
        report = out["report"]
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["warnings"]), 1)
        self.assertEqual(self.calls(out, "duplicate")[0][1:], ["고양이.jpg", "고양이 (ps-remover)"])
        closed = [entry[1] for entry in self.calls(out, "close")]
        self.assertEqual(closed, ["고양이 (ps-remover)"])
        # The user's own document is still open, next to the result.
        self.assertEqual(sorted(d["file"] for d in out["documents"]), sorted([PHOTO, OUTPUT]))

    def test_shapes_are_scaled_to_the_opened_size(self):
        out = self.run_script(remove_config(image_size=(2000, 1500)))
        report = out["report"]
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["warnings"]), 1)
        box = self.actions(out, "setd")[0][2]["T   "]["desc"]
        self.assertEqual((box["Left"]["value"], box["Top "]["value"], box["Rght"]["value"], box["Btom"]["value"]),
                         (200, 100, 600, 500))

    def test_rotated_photo_is_reported(self):
        out = self.run_script(remove_config(image_size=(3000, 4000)))
        self.assertFalse(out["report"]["ok"])
        self.assertIn("EXIF", out["report"]["error"])
        self.assertEqual(self.actions(out, "setd"), [])

    def test_layers_are_merged(self):
        layers = [{"name": "Text", "kind": "TEXT"}, {"name": "Background", "background": True}]
        out = self.run_script(remove_config(), images={PHOTO: {"width": 4000, "height": 3000, "layers": layers}})
        self.assertTrue(out["report"]["ok"])
        self.assertEqual(len(self.calls(out, "mergeVisibleLayers")), 1)
        self.assertEqual(len(out["report"]["warnings"]), 1)

    def test_merge_failure_falls_back_to_flatten(self):
        layers = [{"name": "A"}, {"name": "B"}]
        out = self.run_script(remove_config(), mergeFails=True,
                              images={PHOTO: {"width": 4000, "height": 3000, "layers": layers}})
        self.assertTrue(out["report"]["ok"])
        self.assertEqual(len(self.calls(out, "flatten")), 1)

    def test_single_smart_object_is_rasterized(self):
        layers = [{"name": "Smart", "kind": "SMARTOBJECT"}]
        out = self.run_script(remove_config(), images={PHOTO: {"width": 4000, "height": 3000, "layers": layers}})
        self.assertTrue(out["report"]["ok"])
        self.assertEqual(self.calls(out, "rasterize")[0][1:], ["Smart", "RasterizeType.ENTIRELAYER"])

    def test_indexed_and_32_bit_documents_are_converted(self):
        out = self.run_script(remove_config(), images={PHOTO: {"width": 4000, "height": 3000, "mode": "INDEXEDCOLOR",
                                                                "bits": "THIRTYTWO"}})
        self.assertTrue(out["report"]["ok"])
        self.assertEqual(self.calls(out, "changeMode")[0][2], "ChangeMode.RGB")
        self.assertEqual(self.calls(out, "bitsPerChannel")[0][2], "BitsPerChannelType.SIXTEEN")

    def test_transparent_removal(self):
        config = remove_config(options=api.RemoveOptions(method="transparent"), output="/photos/고양이_removed.png")
        out = self.run_script(config)
        report = out["report"]
        self.assertTrue(report["ok"], report.get("error"))
        self.assertEqual(self.calls(out, "isBackgroundLayer")[0][1:], ["Background", False])
        self.assertEqual(len(self.calls(out, "clear")), 1)
        self.assertEqual(self.actions(out, "Fl  "), [])
        self.assertEqual(self.calls(out, "saveAs")[0][3]["kind"], "png")
        self.assertEqual(report["warnings"], [])

    def test_transparent_removal_to_jpeg_warns(self):
        out = self.run_script(remove_config(options=api.RemoveOptions(method="transparent")))
        self.assertTrue(out["report"]["ok"])
        self.assertIn("흰색", out["report"]["warnings"][0])

    def test_locked_layer_is_unlocked(self):
        layers = [{"name": "Layer 0", "pixelsLocked": True}]
        out = self.run_script(remove_config(), images={PHOTO: {"width": 4000, "height": 3000, "layers": layers}})
        self.assertTrue(out["report"]["ok"])
        self.assertEqual(self.calls(out, "pixelsLocked")[0][1:], ["Layer 0", False])

    def test_16_bit_jpeg_is_saved_from_an_8_bit_copy(self):
        out = self.run_script(remove_config(), images={PHOTO: {"width": 4000, "height": 3000, "bits": "SIXTEEN"}})
        self.assertTrue(out["report"]["ok"])
        save = self.calls(out, "saveAs")[0]
        self.assertEqual(save[1], "고양이_removed")  # the converted copy
        self.assertEqual(save[6], "BitsPerChannelType.EIGHT")
        closed = [entry[1] for entry in self.calls(out, "close")]
        self.assertEqual(closed, ["고양이_removed", "고양이.jpg"])

    def test_cmyk_png_is_saved_from_an_rgb_copy(self):
        out = self.run_script(remove_config(output="/photos/out.png"),
                              images={PHOTO: {"width": 4000, "height": 3000, "mode": "CMYK"}})
        self.assertTrue(out["report"]["ok"])
        self.assertEqual(self.calls(out, "changeMode")[0][1:], ["out", "ChangeMode.RGB"])

    def test_other_formats(self):
        for name, kind in (("out.tif", "tiff"), ("out.TIFF", "tiff"), ("out.psd", "psd"), ("out.jpeg", "jpeg")):
            with self.subTest(name=name):
                out = self.run_script(remove_config(output="/photos/" + name))
                self.assertTrue(out["report"]["ok"])
                self.assertEqual(self.calls(out, "saveAs")[0][3]["kind"], kind)

    def test_unsupported_output_format(self):
        out = self.run_script(remove_config(output="/photos/out.gif"))
        self.assertFalse(out["report"]["ok"])
        self.assertIn("저장 형식", out["report"]["error"])

    def test_missing_output_folder_is_created(self):
        out = self.run_script(remove_config(output="/results/new/out.jpg"), missingFolders=["/results/new"])
        self.assertTrue(out["report"]["ok"])
        self.assertEqual(self.calls(out, "createFolder")[0][1], "/results/new")

    def test_stale_result_document_is_reopened(self):
        out = self.run_script(remove_config(), files=[PHOTO, OUTPUT],
                              openDocuments=[{"file": OUTPUT, "saved": True}])
        self.assertTrue(out["report"]["ok"])
        self.assertEqual([entry[1] for entry in self.calls(out, "close")], ["고양이.jpg", "고양이_removed.jpg"])
        self.assertEqual([d["file"] for d in out["documents"]], [OUTPUT])

    def test_unsaved_result_document_is_kept(self):
        out = self.run_script(remove_config(), files=[PHOTO, OUTPUT],
                              openDocuments=[{"file": OUTPUT, "saved": False}])
        report = out["report"]
        self.assertTrue(report["ok"])
        self.assertNotIn("openedOutput", report)
        self.assertEqual(len(report["warnings"]), 1)

    def test_no_keep_open(self):
        out = self.run_script(remove_config(options=api.RemoveOptions(keep_open=False)))
        self.assertTrue(out["report"]["ok"])
        self.assertEqual(out["documents"], [])
        self.assertEqual(self.calls(out, "bringToFront"), [])

    # ------------------------------------------------------------ subject

    def test_subject_inside_drawn_area(self):
        out = self.run_script(remove_config(options=api.RemoveOptions(subject=True, expand=0)),
                              subject=[150, 100, 1000, 900])
        report = out["report"]
        self.assertTrue(report["ok"])
        self.assertEqual(report["selectionBounds"], [150, 100, 300, 250])  # subject ∩ drawn area
        save_selection = self.actions(out, "Dplc")[0][2]
        self.assertEqual(save_selection["null"], {"ref": [{"cls": "Chnl", "property": "fsel"}]})
        self.assertEqual(save_selection["Nm  "], "ps-remover area")
        self.assertFalse(self.actions(out, "autoCutout")[0][2]["sampleAllLayers"])
        self.assertEqual([e[2] for e in self.calls(out, "loadSelection")], ["SelectionType.INTERSECT"])
        self.assertEqual(len(self.calls(out, "removeChannel")), 1)

    def test_subject_not_found_falls_back_to_drawn_area(self):
        for scenario in ({"subject": [2000, 2000, 2500, 2500]}, {"subjectThrows": True}):
            with self.subTest(scenario=scenario):
                out = self.run_script(remove_config(options=api.RemoveOptions(subject=True, expand=0)), **scenario)
                report = out["report"]
                self.assertTrue(report["ok"])
                self.assertEqual(report["selectionBounds"], [100, 50, 300, 250])
                self.assertEqual(len(report["warnings"]), 1)
                self.assertEqual(self.calls(out, "loadSelection")[-1][2], "SelectionType.REPLACE")
                self.assertEqual(len(self.calls(out, "removeChannel")), 1)

    def test_subject_only(self):
        out = self.run_script(remove_config([], options=api.RemoveOptions(subject=True, expand=0)),
                              subject=[10, 20, 30, 40])
        self.assertTrue(out["report"]["ok"])
        self.assertEqual(out["report"]["selectionBounds"], [10, 20, 30, 40])
        self.assertEqual(self.actions(out, "Dplc"), [])

    def test_subject_only_unsupported(self):
        out = self.run_script(remove_config([], options=api.RemoveOptions(subject=True)), subjectThrows=True)
        self.assertFalse(out["report"]["ok"])
        self.assertIn("CC 2018", out["report"]["error"])

    # ------------------------------------------------------ other actions

    def test_open(self):
        out = self.run_script({"action": "open", "input": PHOTO, "report": "return"})
        self.assertTrue(out["report"]["ok"])
        self.assertEqual(out["report"]["document"]["name"], "고양이.jpg")
        self.assertEqual([d["file"] for d in out["documents"]], [PHOTO])

    def test_remove_current_selection(self):
        config = api.build_remove_current_config(api.RemoveOptions(), output=None)
        doc = {"file": PHOTO, "width": 800, "height": 600, "resolution": 150, "selection": [10, 10, 50, 50]}
        out = self.run_script(config, openDocuments=[doc])
        report = out["report"]
        self.assertTrue(report["ok"], report.get("error"))
        self.assertEqual(report["selectionBounds"], [6, 6, 54, 54])
        self.assertEqual(self.calls(out, "suspendHistory")[0][2], "선택 영역 제거 (ps-remover)")
        self.assertEqual(len(self.actions(out, "Fl  ")), 1)
        self.assertEqual(self.calls(out, "saveAs"), [])
        self.assertEqual(self.calls(out, "close"), [])
        self.assertEqual(out["documents"][0]["resolution"], 150)

    def test_remove_current_selection_and_save(self):
        config = api.build_remove_current_config(api.RemoveOptions(), output="x.png")
        config["output"] = "/photos/x.png"
        out = self.run_script(config, openDocuments=[{"file": PHOTO, "selection": [10, 10, 50, 50]}])
        self.assertTrue(out["report"]["ok"])
        self.assertEqual(out["report"]["output"], "/photos/x.png")
        self.assertEqual(self.calls(out, "saveAs")[0][3]["kind"], "png")

    def test_remove_current_errors(self):
        config = api.build_remove_current_config()
        cases = [
            ({}, "열려 있는 문서가 없습니다"),
            ({"openDocuments": [{"file": PHOTO}]}, "선택 영역이 없습니다"),
            ({"openDocuments": [{"file": PHOTO, "selection": [0, 0, 5, 5], "layers": [{"name": "T", "kind": "TEXT"}]}]},
             "픽셀 레이어가 아닙니다"),
            ({"openDocuments": [{"file": PHOTO, "selection": [0, 0, 5, 5], "layers": [{"name": "L", "allLocked": True}]}]},
             "잠겨 있습니다"),
            ({"openDocuments": [{"file": PHOTO, "selection": [0, 0, 5, 5], "mode": "INDEXEDCOLOR"}]}, "색상 모드"),
        ]
        for scenario, message in cases:
            with self.subTest(message=message):
                out = self.run_script(config, **scenario)
                self.assertFalse(out["report"]["ok"])
                self.assertIn(message, out["report"]["error"])

    def test_error_inside_history_step_is_reported(self):
        config = api.build_remove_current_config()
        out = self.run_script(config, failEvents=["Fl  "],
                              openDocuments=[{"file": PHOTO, "selection": [0, 0, 5, 5], "resolution": 300}])
        self.assertFalse(out["report"]["ok"])
        self.assertIn("not currently available", out["report"]["error"])
        self.assertEqual(out["documents"][0]["resolution"], 300)

    def test_alert_report(self):
        out = self.run_script(remove_config(report="alert"))
        self.assertEqual(len(out["alerts"]), 1)
        message, title = out["alerts"][0]
        self.assertIn("저장 위치: " + OUTPUT, message)
        self.assertEqual(title, "ps-remover")

    def test_unknown_action(self):
        out = self.run_script({"action": "dance", "report": "return"})
        self.assertFalse(out["report"]["ok"])

    def test_brush_selection(self):
        out = self.run_script(remove_config([shapes.brush([(100, 100), (200, 100), (200, 200)], 15)]))
        self.assertTrue(out["report"]["ok"])
        events = [entry[1] for entry in self.actions(out) if entry[1] in ("setd", "AddT")]
        self.assertEqual(events, ["setd", "AddT"])


if __name__ == "__main__":
    unittest.main()
