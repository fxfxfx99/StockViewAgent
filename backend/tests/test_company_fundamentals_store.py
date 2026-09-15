import csv
from types import SimpleNamespace

from app.storage import company_fundamentals_store as store
from app.storage import data_asset_manager


def _write_csv(path, headers, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def test_import_local_2025_files_upserts_and_queries(monkeypatch, tmp_path):
    data_dir = tmp_path / "backend" / "data"
    data_dir.mkdir(parents=True)
    settings = SimpleNamespace(
        data_dir=data_dir,
        enable_data_asset_backups=True,
        data_asset_max_backups=2,
    )
    monkeypatch.setattr(store, "settings", settings)
    monkeypatch.setattr(data_asset_manager, "settings", settings)

    root = data_dir.parent
    _write_csv(
        root / "上市公司基本信息 2025.csv",
        ["Symbol", "ShortName", "EndDate", "ListedCoID", "SecurityID", "FullName", "IndustryCodeC", "IndustryNameC", "PROVINCE", "CITY", "LISTINGDATE", "LISTINGSTATE", "MAINBUSSINESS", "BusinessScope", "Website", "EMAIL"],
        [
            {
                "Symbol": "000001",
                "ShortName": "平安银行",
                "EndDate": "2025-12-31",
                "ListedCoID": "101704",
                "SecurityID": "201000000001",
                "FullName": "平安银行股份有限公司",
                "IndustryCodeC": "J66",
                "IndustryNameC": "货币金融服务",
                "PROVINCE": "广东省",
                "CITY": "深圳市",
                "LISTINGDATE": "1991-04-03",
                "LISTINGSTATE": "正常上市",
                "MAINBUSSINESS": "银行业务",
                "BusinessScope": "银行业务范围",
                "Website": "www.bank.com",
                "EMAIL": "ir@example.com",
            }
        ],
    )
    _write_csv(
        root / "财务指标 2025.csv",
        ["FI_T1.Stkcd", "FI_T1.ShortName", "FI_T1.Accper", "FI_T1.Typrep", "FI_T1.Source", "FI_T1.Indcd1", "FI_T1.Indnme1", "FI_T1.F010101A"],
        [
            {
                "FI_T1.Stkcd": "000001",
                "FI_T1.ShortName": "平安银行",
                "FI_T1.Accper": "2025-12-31",
                "FI_T1.Typrep": "A",
                "FI_T1.Source": "0",
                "FI_T1.Indcd1": "J66",
                "FI_T1.Indnme1": "货币金融服务",
                "FI_T1.F010101A": "1.23",
            }
        ],
    )
    _write_csv(
        root / "管理层经营讨论与分析 2025.csv",
        ["Symbol", "ShortName", "Enddate", "IndustryCode1", "IndustryName1", "ManaDiscAnal", "TextualSimilarity", "PositiveVocabularyNum", "NegativeVocabularyNum", "TotalWordsNum", "SentencesNum", "WordsNum", "EmotionTone1", "EmotionTone2"],
        [
            {
                "Symbol": "000001",
                "ShortName": "平安银行",
                "Enddate": "2025-06-30",
                "IndustryCode1": "J66",
                "IndustryName1": "货币金融服务",
                "ManaDiscAnal": "总体经营情况良好。",
                "TextualSimilarity": "0.88",
                "PositiveVocabularyNum": "10",
                "NegativeVocabularyNum": "2",
                "TotalWordsNum": "100",
                "SentencesNum": "3",
                "WordsNum": "98",
                "EmotionTone1": "0.1",
                "EmotionTone2": "0.2",
            }
        ],
    )

    first = store.import_local_2025_files(root=root)
    second = store.import_local_2025_files(root=root)
    latest = store.latest_for_symbol("000001.SZ")
    status = store.status()

    assert first["basic_rows"] == 1
    assert first["financial_rows"] == 1
    assert first["mda_rows"] == 1
    assert second["basic_rows"] == 0
    assert status["counts"] == {"basic_info": 1, "financial": 1, "mda": 1}
    assert latest["basic_info"]["full_name"] == "平安银行股份有限公司"
    assert latest["financial"]["report_type"] == "A"
    assert latest["mda"]["mana_disc_anal"] == "总体经营情况良好。"
