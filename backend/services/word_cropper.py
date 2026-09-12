import json


def crop_words(
    input_file,
    start_time,
    end_time,
    output_file
):

    with open(
        input_file,
        "r",
        encoding="utf-8"
    ) as f:
        words = json.load(f)


    cropped = []


    for word in words:

        word_start = word["start"]
        word_end = word["end"]


        # κρατάμε μόνο λέξεις μέσα στο clip
        if word_end < start_time:
            continue

        if word_start > end_time:
            break


        cropped.append({

            # IMPORTANT:
            # μετατροπή σε νέο timeline
            "start": max(
                0,
                word_start - start_time
            ),

            "end": min(
                end_time - start_time,
                word_end - start_time
            ),

            "word": word["word"]

        })


    with open(
        output_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            cropped,
            f,
            ensure_ascii=False,
            indent=2
        )


    print(
        f"Cropped words saved: {output_file}",
        flush=True
    )


    return output_file