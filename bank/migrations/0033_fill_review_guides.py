"""Fill each chapter's Final review guide (what belongs there). Only empty guides are filled,
so edits made in Admin are never overwritten."""
from django.db import migrations

GUIDES = {
    "Physiology": {
        "Homeostasis": "Definition of physiology, internal environment, homeostasis, feedback control (negative and positive feedback, gain), cell structure and functions of organelles.",
        "Transport across cell membrane": "Cell membrane structure; diffusion, facilitated diffusion, osmosis, primary and secondary active transport, Na+/K+ pump, channels, endocytosis and exocytosis. Membrane and action potentials go to Excitable tissue.",
        "Body fluids": "Total body water, body fluid compartments (ICF, ECF, plasma, interstitial) and their measurement, composition, osmolarity and fluid shifts, edema, water balance and its regulation (thirst, ADH).",
        "Excitable tissue": "Resting membrane potential, action potential, nerve fibres and conduction, synaptic transmission, EPSP/IPSP, neuromuscular junction and its drugs and diseases, skeletal and smooth muscle, excitation-contraction coupling, molecular basis of contraction.",
        "ANS": "Sympathetic and parasympathetic organisation, autonomic neurotransmitters and receptors, effects of sympathetic and parasympathetic stimulation on organs, autonomic drugs and blockers, Horner's syndrome.",
        "Temperature": "Body temperature, heat production and loss, hypothalamic regulation, fever, hypothermia and hyperthermia, metabolic rate and BMR with the factors affecting them.",
        "Hematology": "Blood and plasma, plasma proteins, red cells, erythropoiesis and its control, hemoglobin, RBC destruction, anemias, white cells, immunity, blood groups (ABO, Rh) and transfusion, platelets, hemostasis and coagulation, anticoagulants, bleeding tests and bleeding disorders.",
        "CVS": "Cardiac muscle properties, conducting system, ECG (normal and abnormal), myocardial infarction, cardiac cycle and heart sounds, cardiac output, venous return, arterial blood pressure and its control, local regulation of blood flow, special circulations (coronary, cerebral, skin, etc.), cardiovascular responses to exercise, heart failure and shock.",
        "Respiratory": "Mechanics of breathing, lung volumes and lung function tests, compliance and surfactant, airway resistance, gas exchange and ventilation-perfusion, O2 and CO2 transport and dissociation curves, control of respiration, hypoxia and cyanosis, responses to altitude, diving and exercise.",
        "GIT": "Mouth and saliva, swallowing and esophagus, gastric secretion and its control, gastric motility and emptying, vomiting, exocrine pancreas, liver and bile, gallbladder, small and large intestine, intestinal motility, digestion and absorption, GI hormones.",
        "Endocrine": "General hormone mechanisms and receptors, hypothalamus and pituitary, growth hormone, thyroid, adrenal cortex and medulla, endocrine pancreas (insulin, glucagon) and blood glucose, calcium regulation (PTH, vitamin D, calcitonin). Sex hormones, pregnancy and lactation go to Reproductive.",
        "Renal": "Kidney functions, renal blood flow, GFR and its regulation, tubular reabsorption and secretion, clearance, urine concentration and the countercurrent mechanism, renal handling of Na+, K+ and H+, renal acid-base regulation, micturition. Body water compartments and edema go to Body fluids.",
        "Reproductive": "Male reproduction (spermatogenesis, testosterone), female reproduction (ovarian and menstrual cycles, estrogen, progesterone), fertilisation, pregnancy and placental hormones, parturition, lactation, contraception, puberty and menopause.",
    },
    "Biochemistry": {
        "Cell": "Cell membrane composition and structure, organelles and their biochemical functions, membrane transport at the molecular level. Laboratory methods go to Biochemical techniques.",
        "Biochemical techniques": "Laboratory methods: chromatography, electrophoresis, centrifugation and cell fractionation, spectrophotometry, immunoassays (ELISA, RIA), isotopes. DNA techniques (PCR, blotting, recombinant DNA) go to Genetics.",
        "Water and pH": "Properties of water, pH, pKa, buffers and Henderson-Hasselbalch, body buffer systems, acid-base balance and its disorders.",
        "Carbohydrates": "CHEMISTRY only: classification, structure, isomerism, monosaccharides, disaccharides, polysaccharides, glycosaminoglycans, proteoglycans and glycoproteins, sugar reactions. Any pathway (glycolysis, glycogen, gluconeogenesis, etc.) goes to Carbohydrates metabolism.",
        "Lipids": "CHEMISTRY only: classification, fatty acids, triacylglycerols, phospholipids, sphingolipids and sphingolipidoses, steroids and cholesterol structure, eicosanoid structure, lipoprotein structure (classes, density, electrophoresis, composition, which apolipoprotein is in which particle). Pathways and lipoprotein functions go to Lipids metabolism.",
        "Proteins": "CHEMISTRY only: amino acid structure, classification and properties, peptides (including beta-endorphin), levels of protein structure, denaturation, fibrous and globular proteins, collagen, hemoglobin and myoglobin structure and oxygen binding. Amino acid breakdown and the urea cycle go to Proteins and amino acid metabolism.",
        "Enzymes": "Enzyme classification, coenzymes, mechanism of action, kinetics (Km, Vmax, Lineweaver-Burk), inhibition, regulation, isoenzymes, clinical (diagnostic) enzymes.",
        "Nucleotides": "Structure of nucleotides and nucleic acids, DNA and RNA structure, types of RNA, biologically important nucleotides. Purine/pyrimidine synthesis and breakdown go to Purines & Pyramidines; replication, transcription and translation go to Genetics.",
        "Vitamins": "Fat- and water-soluble vitamins: sources, active and coenzyme forms, functions, deficiency diseases and toxicity.",
        "Carbohydrates metabolism": "Digestion and absorption of carbohydrates, glycolysis, pyruvate dehydrogenase, TCA cycle, electron transport chain and oxidative phosphorylation, gluconeogenesis, glycogen metabolism and glycogen storage diseases, HMP shunt, galactose and fructose metabolism, regulation of blood glucose, diabetes biochemistry.",
        "Lipids metabolism": "Digestion and absorption of lipids, beta-oxidation, ketone bodies, fatty acid synthesis, triacylglycerol and phospholipid metabolism, cholesterol synthesis, lipoprotein FUNCTIONS (apolipoproteins as activators or ligands, lipoprotein lipase, LCAT, receptors, remnants), hyperlipoproteinemias, abetalipoproteinemia, fatty liver, eicosanoid synthesis.",
        "Proteins and amino acid metabolism": "Protein digestion and absorption, transamination, deamination, ammonia and the urea cycle, metabolism of individual amino acids, special products from amino acids, inborn errors of amino acid metabolism (PKU, alkaptonuria, MSUD, etc.).",
        "Heme metabolism": "Porphyrin and heme synthesis, porphyrias, heme breakdown, bilirubin metabolism and jaundice, iron metabolism.",
        "Hormones": "Biochemistry of hormones: classification, receptors, second messengers and signalling, synthesis and actions of individual hormones.",
        "Nutrition": "Energy requirements and BMR, macronutrients, protein quality, dietary fibre, balanced diet, malnutrition (kwashiorkor, marasmus), obesity, minerals and trace elements.",
        "Plasma proteins": "Albumin, globulins, plasma protein electrophoresis, immunoglobulins, acute-phase and transport proteins, clotting factors as proteins.",
        "Purines & Pyramidines": "De novo and salvage synthesis of purines and pyrimidines, their breakdown, uric acid and gout, Lesch-Nyhan syndrome, orotic aciduria, related drugs.",
        "Genetics": "DNA replication and repair, transcription, RNA processing, the genetic code, translation, regulation of gene expression, mutations, recombinant DNA, PCR, blotting, gene therapy.",
        "Cancer Biochemistry": "Oncogenes, tumour suppressor genes, carcinogens, tumour markers, biochemistry of the cell cycle in cancer.",
        "Xenobiotics metabolism": "Detoxification of foreign compounds: phase I (cytochrome P450, oxidation, reduction, hydrolysis) and phase II (conjugation) reactions.",
    },
    "Anatomy": {
        "Thorax": "Gross anatomy of the thoracic wall, breast, diaphragm, pleura and lungs, mediastinum, heart and great vessels, thoracic esophagus, trachea, thoracic nerves and lymphatics. Development goes to Embryology chapters; microscopic structure to Histology.",
        "Abdomen": "Gross anatomy of the anterior abdominal wall and inguinal canal, peritoneum, stomach, intestines, liver, gallbladder, pancreas, spleen, kidneys, suprarenal glands, abdominal ureters, posterior abdominal wall, abdominal vessels, nerves and lymphatics.",
        "Pelvis and Perineum": "Bony pelvis, pelvic floor, pelvic organs (bladder, rectum, uterus, ovaries, prostate, etc.), pelvic vessels and nerves, perineum, external genitalia, anal canal, pudendal canal.",
        "Upper Limb": "Gross anatomy of the upper limb (pectoral region, axilla, shoulder, arm, forearm, hand), brachial plexus, and the back (vertebral column, back muscles, spinal cord and meninges).",
        "Lower Limb": "Gross anatomy of the gluteal region, thigh, knee, leg, ankle and foot, lumbosacral plexus nerves of the limb, femoral triangle, popliteal fossa, arches of the foot.",
        "Head and Neck": "Gross anatomy of the skull, scalp, face, cranial cavity and meninges, cranial nerves, orbit, ear, nose, oral cavity, pharynx, larynx, neck triangles, thyroid and parathyroids, head and neck vessels and lymphatics.",
        "General Embryology": "Gametogenesis, fertilisation, cleavage, implantation, bilaminar and trilaminar disc, gastrulation, neurulation, somites, germ layer derivatives, folding, fetal membranes, placenta, twins, teratology and birth defects in general.",
        "development of Circulatory System": "Development of the heart, heart tube and septation, arteries (aortic arches) and veins, fetal circulation and changes at birth, congenital heart and vessel defects.",
        "Development of the GIT": "Development of the gut tube, foregut, midgut and hindgut, rotation, liver, pancreas and spleen development, mesenteries, and their congenital anomalies.",
        "Respiratory System": "Development of the larynx, trachea and lungs, tracheoesophageal fistula, stages of lung maturation, surfactant at birth and related anomalies.",
        "Urogenital System": "Development of the kidneys and urinary tract, gonads, genital ducts and external genitalia, and their anomalies.",
        "Head and Neck development": "Pharyngeal arches, pouches, clefts and their derivatives, face, palate, tongue, thyroid development, cleft lip and palate and other head and neck anomalies.",
        "Central nervous system development": "Neural tube and brain vesicles, spinal cord and brain development, neural crest, neural tube defects and other CNS anomalies.",
        "Endocrine development": "Development of the pituitary, thyroid, parathyroids, suprarenal glands and pancreatic islets, and their anomalies.",
        "Limbs development": "Limb buds, apical ectodermal ridge, zone of polarising activity, limb rotation, bone and muscle development of the limbs, limb anomalies.",
        "Special Senses development": "Development of the eye and ear and their anomalies.",
        "Epithelial": "Histology of epithelium: types, surface specialisations (microvilli, cilia), cell junctions, basement membrane, glands.",
        "Connective Tissue": "Histology of connective tissue proper (cells, fibres, ground substance), adipose tissue, cartilage, bone and ossification, blood cells and hemopoiesis.",
        "Muscular Tissue": "Histology of skeletal, cardiac and smooth muscle.",
        "Nervous tissue": "Histology of neurons, neuroglia, nerve fibres and myelin, peripheral nerves, ganglia, synapses.",
        "Circulatory System": "Histology of the heart wall, arteries, veins, capillaries and lymphatic vessels.",
        "Respiratory": "Histology of the nasal cavity, larynx, trachea, bronchi, bronchioles and alveoli.",
        "Digestive System": "Histology of the oral cavity, tongue, teeth, esophagus, stomach, intestines, liver, gallbladder, pancreas and salivary glands.",
        "Immune System": "Histology of lymphoid organs: lymph nodes, spleen, thymus, tonsils, MALT, and immune cells.",
        "Endocrine system": "Histology of the pituitary, thyroid, parathyroid, suprarenal glands, pineal and pancreatic islets.",
        "Histology of special senses": "Histology of the eye and ear.",
        "Integumentary System": "Histology of skin, hair, nails and skin glands.",
        "Reproductive System": "Histology of male and female reproductive organs (testis, ducts, prostate, ovary, uterine tube, uterus, placenta, mammary gland).",
        "Urinary System": "Histology of the kidney, nephron, ureter, bladder and urethra.",
    },
}


def fill(apps, schema_editor):
    QuestionBank = apps.get_model("bank", "QuestionBank")
    for subject, guides in GUIDES.items():
        for title, guide in guides.items():
            QuestionBank.objects.filter(subject__name=subject, title=title, review_guide="").update(review_guide=guide)


class Migration(migrations.Migration):
    dependencies = [("bank", "0032_deck_review_guide")]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
