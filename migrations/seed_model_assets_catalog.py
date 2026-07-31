import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ngss.db"
NOW = int(time.time())

CATALOG = [
    {
        "model_id": "CELL_MODEL_01",
        "src": "model_assets/ms-ls1-2a-cell-membrane-boundary-01.svg",
        "title": "Cell Membrane Boundary",
        "alt_text": "A cell diagram highlighting the cell membrane as the outer boundary around the cell.",
        "caption": "Supports questions about identifying the cell membrane and its boundary function.",
    },
    {
        "model_id": "CELL_MODEL_02",
        "src": "model_assets/ms-ls1-2a-nucleus-genetic-information-01.svg",
        "title": "Nucleus and Genetic Information",
        "alt_text": "A cell diagram highlighting a large nucleus inside the cell where genetic information is stored.",
        "caption": "Supports questions about the nucleus and its role in storing genetic information.",
    },
    {
        "model_id": "CELL_MODEL_03",
        "src": "model_assets/ms-ls1-2a-cytoplasm-region-01.svg",
        "title": "Cytoplasm Region",
        "alt_text": "A cell diagram shading the cytoplasm between the nucleus and the cell membrane.",
        "caption": "Supports questions about identifying cytoplasm in a cell model.",
    },
    {
        "model_id": "CELL_MODEL_04",
        "src": "model_assets/ms-ls1-2a-plant-cell-labeled-structures-01.svg",
        "title": "Labeled Plant Cell Structures",
        "alt_text": "A plant cell diagram labeled with nucleus, chloroplasts, cell wall, cell membrane, cytoplasm, and mitochondria.",
        "caption": "Supports questions about how plant cell structures work together.",
    },
    {
        "model_id": "CELL_MODEL_05",
        "src": "model_assets/ms-ls1-2a-animal-cell-no-wall-chloroplasts-01.svg",
        "title": "Animal Cell Without Cell Wall or Chloroplasts",
        "alt_text": "An animal cell diagram showing common cell parts without chloroplasts or a cell wall.",
        "caption": "Supports questions comparing animal cell models with plant cell models.",
    },
    {
        "model_id": "CELL_MODEL_06",
        "src": "model_assets/ms-ls1-2a-plant-cell-wall-chloroplasts-01.svg",
        "title": "Plant Cell Wall and Chloroplasts",
        "alt_text": "A plant cell diagram with many green chloroplasts and a rigid outer cell wall.",
        "caption": "Supports questions about evidence that a model represents a plant cell.",
    },
    {
        "model_id": "CELL_MODEL_07",
        "src": "model_assets/ms-ls1-2b-cell-system-interactions-01.svg",
        "title": "Cell as a System",
        "alt_text": "A cell system diagram showing cell parts connected by arrows to represent interactions among structures.",
        "caption": "Supports questions about cells as systems made of interacting parts.",
    },
    {
        "model_id": "CELL_MODEL_08",
        "src": "model_assets/ms-ls1-2b-nucleus-controls-cell-functions-01.svg",
        "title": "Nucleus Connections to Cell Structures",
        "alt_text": "A cell diagram showing the nucleus connected by arrows to several other cell structures.",
        "caption": "Supports questions about how the nucleus helps control cell activities.",
    },
    {
        "model_id": "CELL_MODEL_09",
        "src": "model_assets/ms-ls1-2b-plant-cell-wall-membrane-01.svg",
        "title": "Plant Cell Wall and Cell Membrane",
        "alt_text": "A plant cell diagram showing the cell wall outside the cell membrane.",
        "caption": "Supports questions about how plant cell structures provide support and regulate movement.",
    },
    {
        "model_id": "CELL_MODEL_10",
        "src": "model_assets/ms-ls1-2b-chloroplast-mitochondria-energy-01.svg",
        "title": "Chloroplasts and Mitochondria Energy Model",
        "alt_text": "A plant cell diagram showing chloroplasts making food and mitochondria using food to release energy.",
        "caption": "Supports questions about energy-related interactions inside plant cells.",
    },
    {
        "model_id": "BODY_MODEL_01",
        "src": "model_assets/ms-ls1-3a-cells-tissues-organs-systems-01.svg",
        "title": "Cells to Organ Systems",
        "alt_text": "A biological organization model showing cells grouped into tissues, tissues into organs, and organs into an organ system.",
        "caption": "Supports questions about levels of organization in multicellular organisms.",
    },
    {
        "model_id": "BODY_MODEL_02",
        "src": "model_assets/ms-ls1-3a-stomach-tissues-digestive-system-01.svg",
        "title": "Stomach Tissues in the Digestive System",
        "alt_text": "A model of the stomach showing multiple tissue layers and connections to other digestive organs.",
        "caption": "Supports questions about organs, tissues, and organ systems.",
    },
    {
        "model_id": "BODY_MODEL_03",
        "src": "model_assets/ms-ls1-3b-digestive-system-organs-01.svg",
        "title": "Digestive System Organs",
        "alt_text": "A body system model highlighting digestive organs such as the stomach, small intestine, and large intestine.",
        "caption": "Supports questions about the digestive system and nutrient absorption.",
    },
    {
        "model_id": "BODY_MODEL_04",
        "src": "model_assets/ms-ls1-3b-circulatory-system-heart-vessels-01.svg",
        "title": "Circulatory System",
        "alt_text": "A body system model highlighting the heart and blood vessels of the circulatory system.",
        "caption": "Supports questions about moving blood, oxygen, nutrients, and wastes through the body.",
    },
    {
        "model_id": "BODY_MODEL_05",
        "src": "model_assets/ms-ls1-3b-respiratory-system-lungs-01.svg",
        "title": "Respiratory System",
        "alt_text": "A body system model highlighting the lungs and airways of the respiratory system.",
        "caption": "Supports questions about oxygen intake and carbon dioxide removal.",
    },
    {
        "model_id": "BODY_MODEL_06",
        "src": "model_assets/ms-ls1-3b-nervous-system-brain-nerves-01.svg",
        "title": "Nervous System",
        "alt_text": "A body system model highlighting the brain, spinal cord, and nerves.",
        "caption": "Supports questions about receiving information, sending signals, and controlling responses.",
    },
    {
        "model_id": "BODY_MODEL_07",
        "src": "model_assets/ms-ls1-3b-muscular-system-movement-01.svg",
        "title": "Muscular System",
        "alt_text": "A body system model highlighting muscles involved in moving body parts.",
        "caption": "Supports questions about how muscles work with bones to create movement.",
    },
    {
        "model_id": "BODY_MODEL_08",
        "src": "model_assets/ms-ls1-3b-skeletal-system-support-protection-01.svg",
        "title": "Skeletal System",
        "alt_text": "A body system model highlighting bones such as the skull, ribs, and backbone.",
        "caption": "Supports questions about support, protection, and movement functions of the skeleton.",
    },
    {
        "model_id": "BODY_MODEL_09",
        "src": "model_assets/ms-ls1-3c-digestive-respiratory-circulatory-exchange-01.svg",
        "title": "Digestive, Respiratory, and Circulatory System Exchange",
        "alt_text": "A body systems model showing oxygen moving from lungs to blood, nutrients moving from intestines to blood, and blood delivering materials to body cells.",
        "caption": "Supports questions about interactions among digestive, respiratory, and circulatory systems.",
    },
    {
        "model_id": "BODY_MODEL_10",
        "src": "model_assets/ms-ls1-3c-nervous-muscular-skeletal-interaction-01.svg",
        "title": "Nervous, Muscular, and Skeletal System Interaction",
        "alt_text": "A body systems model showing the brain and nerves sending signals to leg muscles that move bones.",
        "caption": "Supports questions about coordinated movement across nervous, muscular, and skeletal systems.",
    },
    {
        "model_id": "MS-LS1-1B_model_cell_theory_basic_01",
        "src": "model_assets/ms-ls1-1b-model-cell-theory-basic-01.png",
        "title": "Basic Cell Theory Model",
        "alt_text": "A model showing living things made of cells to support basic cell theory.",
        "caption": "Supports questions about evidence for the idea that organisms are made of cells.",
    },
    {
        "model_id": "MS-LS1-1B_model_leaf_cells_01",
        "src": "model_assets/ms-ls1-1b-leaf-cells-01.svg",
        "title": "Leaf Cells Model",
        "alt_text": "A model zooming from a leaf to many smaller cells that make up leaf tissue.",
        "caption": "Supports questions about plant structures being made of cells.",
    },
    {
        "model_id": "MS-LS1-1B_model_cells_to_tissue_01",
        "src": "model_assets/ms-ls1-1b-cells-to-tissue-01.svg",
        "title": "Cells Form Tissue",
        "alt_text": "A model showing many small cells grouped together to form tissue.",
        "caption": "Supports questions connecting cell groups to tissues.",
    },
    {
        "model_id": "MS-LS1-1B_model_plant_animal_cells_01",
        "src": "model_assets/ms-ls1-1b-plant-animal-cells-01.svg",
        "title": "Plant and Animal Cells",
        "alt_text": "A comparison model showing plant and animal cells with shared and different structures.",
        "caption": "Supports questions comparing cells from plants and animals.",
    },
    {
        "model_id": "MS-LS1-1B_model_organization_levels_01",
        "src": "model_assets/ms-ls1-1b-organization-levels-01.svg",
        "title": "Levels of Biological Organization",
        "alt_text": "A model showing cells, tissues, organs, organ systems, and an organism as levels of organization.",
        "caption": "Supports questions about how cells contribute to larger body structures.",
    },
    {
        "model_id": "MS-LS1-1B_model_unknown_samples_01",
        "src": "model_assets/ms-ls1-1b-unknown-cell-samples-01.svg",
        "title": "Unknown Samples Under a Microscope",
        "alt_text": "A microscope model comparing unknown samples, some with visible cells and some without visible cells.",
        "caption": "Supports questions about using cell evidence to identify living or once-living material.",
    },
    {
        "model_id": "MS-LS1-1B_model_seedling_cell_division_01",
        "src": "model_assets/ms-ls1-1b-seedling-cell-division-01.svg",
        "title": "Seedling Growth and Cell Division",
        "alt_text": "A model showing a seedling growing over time with a zoomed view of dividing cells near the root tip.",
        "caption": "Supports questions connecting growth to cells dividing and producing more cells.",
    },
    {
        "model_id": "MS-LS1-1B_model_brick_wall_cells_01",
        "src": "model_assets/ms-ls1-1b-brick-wall-cells-01.svg",
        "title": "Brick Wall Cell Analogy",
        "alt_text": "A model comparing many cells in tissue to many bricks arranged together in a wall.",
        "caption": "Supports questions about how many small cells can make up a larger structure.",
    },
]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


with sqlite3.connect(DB) as conn:
    conn.row_factory = sqlite3.Row

    table_exists = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'model_assets'
        LIMIT 1
        """
    ).fetchone()
    require(table_exists, "model_assets table is missing. Run add_model_assets_table.py first.")

    db_model_ids = {
        row["model_id"]
        for row in conn.execute(
            """
            SELECT DISTINCT model_id
            FROM question_metadata
            WHERE model_id IS NOT NULL
              AND TRIM(model_id) <> ''
            """
        )
    }
    catalog_model_ids = {row["model_id"] for row in CATALOG}

    missing_from_catalog = sorted(db_model_ids - catalog_model_ids)
    extra_in_catalog = sorted(catalog_model_ids - db_model_ids)
    require(
        not missing_from_catalog,
        "Catalog missing model_id(s): " + ", ".join(missing_from_catalog),
    )
    require(
        not extra_in_catalog,
        "Catalog includes model_id(s) not present in question_metadata: "
        + ", ".join(extra_in_catalog),
    )

    inserted = 0
    updated = 0
    for row in CATALOG:
        existing = conn.execute(
            "SELECT 1 FROM model_assets WHERE model_id = ?",
            (row["model_id"],),
        ).fetchone()
        if existing:
            updated += 1
        else:
            inserted += 1

        conn.execute(
            """
            INSERT INTO model_assets
              (model_id, asset_type, src, alt_text, title, caption, created_at, updated_at)
            VALUES (?, 'image', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_id) DO UPDATE SET
              asset_type = excluded.asset_type,
              src = excluded.src,
              alt_text = excluded.alt_text,
              title = excluded.title,
              caption = excluded.caption,
              updated_at = excluded.updated_at
            """,
            (
                row["model_id"],
                row["src"],
                row["alt_text"],
                row["title"],
                row["caption"],
                NOW,
                NOW,
            ),
        )

    conn.commit()

print(
    f"Seeded model_assets catalog: {inserted} inserted, {updated} updated, "
    f"{len(CATALOG)} total catalog rows."
)
