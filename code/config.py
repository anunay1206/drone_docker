class Config:

    # INPUT
    # Update this to the folder containing your multiple ortho images
    ORTHO_PATH = "data/examples/s4_tree.tif"
    WORKDIR = "output"

    # DETECTREE
    DETECTREE_MODEL = "models/urban_trees_Cambridge_20230630.pth"
    

    TILE_SIZE = 40
    BUFFER = 5
    IOU_THRESHOLD = 0.5
    CONF_THRESHOLD = 0.35


    # FEATURES + CLUSTERING
    STEP1_OUTPUT = "output/step1_output"

    MODEL_NAME = "vit_base_patch14_dinov2.lvd142m"
    IMG_SIZE = 224
    BATCH_SIZE = 16
    PCA_COMPONENTS = 50

    K_LIST = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20]
    COPY_TO_CLUSTER_FOLDERS = True

    # SPECIES
    CHOSEN_K = 4
    STEP2_OUTPUT = "output/step2_output"


    # VALIDATION
    GROUND_TRUTH_CSV = "data/ground_truth"
    STEP3_VALIDATION_OUTPUT = "output/step3_output"


    # KMZ
    STEP4_OUTPUT = "output/step4_output"
    SOURCE_EPSG = 32643

    COLOR_PALETTE = [
        "990000ff",
        "9900ff00",
        "99ff0000",
        "9900ffff",
        "99ff00ff",
        "99ff8800",
        "9900ffff",
        "99ffffff",
    ]
