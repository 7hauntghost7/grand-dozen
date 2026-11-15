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
        "Cell": "Oraby part 1 ch. 9 (cell and cell membranes): membrane composition and structure, membrane transport, organelles and their biochemical functions; also biochemistry of teeth (ch. 10). Laboratory methods go to Biochemical techniques.",
        "Biochemical techniques": "Laboratory methods, including Oraby's techniques for separating amino acids and proteins (chromatography, electrophoresis, centrifugation, salting out, dialysis) and other assays (spectrophotometry, ELISA, RIA, isotopes). DNA techniques (PCR, blotting, recombinant DNA) go to Genetics.",
        "Water and pH": "Oraby part 1 ch. 12 (physical chemistry): water, pH, pKa, buffers and Henderson-Hasselbalch, acid-base balance and its disorders, osmosis and osmotic pressure, colloids, surface tension, solutions.",
        "Carbohydrates": "CHEMISTRY only (Oraby part 1 ch. 1): monosaccharides, cyclic structure, asymmetric carbon and isomerism, sugar reactions and derivatives, disaccharides, polysaccharides, proteoglycans (glycosaminoglycans) and glycoproteins. Any digestion or pathway goes to Carbohydrates metabolism.",
        "Lipids": "CHEMISTRY only (Oraby part 1 ch. 2): classification, fatty acids, simple lipids and triacylglycerols, complex lipids (phospholipids, glycolipids, sphingolipids and sphingolipidoses), sterols and steroids, amphipathic lipids, free radicals and antioxidants, and lipoprotein STRUCTURE (classes, density, electrophoresis, composition, which apolipoprotein is in which particle). Pathways and lipoprotein functions go to Lipids metabolism.",
        "Proteins": "CHEMISTRY only (Oraby part 1 ch. 3): amino acids (structure, classification, properties), peptides (including beta-endorphin), protein structure levels and denaturation, simple, conjugated and derived proteins, hemoproteins (hemoglobin and myoglobin structure, oxygen binding, abnormal hemoglobins). Separation techniques go to Biochemical techniques; amino acid breakdown to Proteins and amino acid metabolism.",
        "Enzymes": "Oraby part 1 ch. 5: definitions and coenzymes, classification, mechanism of action, kinetics and factors affecting activity, inhibitors, regulation of enzyme activity, isoenzymes, enzymes in clinical diagnosis.",
        "Nucleotides": "Oraby part 1 ch. 4 (chemistry of nucleoproteins): nucleoproteins and nucleotides, free nucleotides of biological importance (ATP, cAMP, NAD, FAD, SAM, PAPS, UDP-glucose), structure of nucleic acids (DNA, RNA types). Synthesis and breakdown go to Purines & Pyramidines; replication, transcription and translation to Genetics.",
        "Vitamins": "Oraby part 1 ch. 6: fat-soluble (A, D, E, K) and water-soluble (B group, C) vitamins: sources, active forms, functions, deficiency and toxicity. Minerals go to Nutrition.",
        "Carbohydrates metabolism": "Oraby part 2 ch. 1, 2 and 4: bioenergetics and biologic oxidation (respiratory chain, oxidative phosphorylation, uncouplers), digestion and absorption of carbohydrates, glycolysis, oxidative decarboxylation of pyruvate, citric acid cycle, pentose phosphate pathway, uronic acid pathway, glycogen metabolism and storage diseases, gluconeogenesis, fructose and galactose metabolism, blood glucose and its regulation, hypoglycemia, diabetes mellitus, glycosuria, integration of metabolism (fed and fasting states).",
        "Lipids metabolism": "Oraby part 2 ch. 3: digestion and absorption of lipids, fate of absorbed lipids, storage and mobilisation, lipogenesis, lipolysis, fatty acid oxidation, sources and fate of acetyl CoA, fatty acid synthesis, eicosanoids, metabolism of conjugated lipids, ketone bodies, cholesterol metabolism, plasma lipoprotein FUNCTIONS and metabolism (apolipoproteins as activators or ligands, lipoprotein lipase, LCAT, receptors, remnants), hyperlipoproteinemias, atherosclerosis, fatty liver.",
        "Proteins and amino acid metabolism": "Protein digestion and absorption, transamination, deamination, ammonia and the urea cycle, metabolism of individual amino acids, special products from amino acids, inborn errors of amino acid metabolism (PKU, alkaptonuria, MSUD, etc.).",
        "Heme metabolism": "Porphyrin and heme synthesis, porphyrias, heme breakdown, bilirubin metabolism and jaundice, iron metabolism.",
        "Hormones": "Oraby part 2 ch. 6: classification of hormones, receptors and mechanisms of action (second messengers), biochemistry of individual hormones.",
        "Nutrition": "Oraby part 1 ch. 11: nutrient requirements, energy needs and BMR, macronutrients and micronutrients (including minerals and trace elements), protein quality, dietary fibre, dietary recommendations, obesity and malnutrition.",
        "Plasma proteins": "Albumin, globulins, plasma protein electrophoresis, immunoglobulins, acute-phase and transport proteins, clotting factors as proteins.",
        "Purines & Pyramidines": "De novo and salvage synthesis of purines and pyrimidines, their breakdown, uric acid and gout, Lesch-Nyhan syndrome, orotic aciduria, related drugs.",
        "Genetics": "DNA replication and repair, transcription, RNA processing, the genetic code, translation, regulation of gene expression, mutations, recombinant DNA, PCR, blotting, gene therapy.",
        "Cancer Biochemistry": "Oraby part 2 ch. 5: cancer, carcinogens, oncogenes and tumour suppressor genes, tumour markers, apoptosis.",
        "Xenobiotics metabolism": "Oraby part 1 ch. 8: detoxification of foreign compounds, phase I (cytochrome P450 oxidation, reduction, hydrolysis) and phase II (conjugation) reactions.",
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
