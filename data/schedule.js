/*
 * お給仕情報の編集ファイルです。
 *
 * - roster: フィルターに表示する在籍メイド一覧。並びは公式サイト
 *   https://akibazettai.com/staff/ の掲載順そのまま（ひかりさんが先頭、
 *   キッチンにゃんこ5名が末尾）。入れ替わったら公式に合わせ直してください。
 *
 *   公式サイトは `<li class="shopNumNN">` の中に `<p>名前</p>` を持ち、NN が
 *   所属店です。取り直して roster と突き合わせると、抜けと配属替えが一度に見えます。
 *   実際に「サイトには載っているのに roster にいない」方（まひろさん）が
 *   長らく抜けたままで、予測から丸ごと外れていました。
 *
 * - kitchenStaff: メイド服を着ないキッチンにゃんこ（roster の部分集合）
 * - homeStore: 公式サイトに載っている配属店（s1〜s4）。記念日の主役をどこに
 *   立たせるかに使います。実際に多く入る店とは別物で、公式の所属です。
 * - schedule: "YYYY-MM-DD" ごとの昼・夜のお給仕情報
 * - featured: 記念日・生誕の主役だけ true
 * - eventLabel: 主役のツールチップ・読み上げ用イベント名
 *
 * 名前を追加・変更するときは roster と schedule の表記を完全に一致させてください。
 * 編集後は `node tests/validate-schedule.js` を実行してください。
 */
window.SCHEDULE_DATA = {
  initialMonth: "2026-09",
  defaultDateFrom: "2026-09-01",
  defaultDateTo: "2026-09-30",
  lastUpdated: "2026年9月2日 1:18 JST時点",

  // Original placeholder artwork, not portraits or confirmed personal preferences.
  // A date-specific entry in events overrides maids; neither creates a shift.
  eventImages: {
    fallback: {
      src: "assets/events/flower.svg",
      alt: "イベントを飾るオリジナルの花の仮イラスト"
    },
    maids: {
      "もなか": { src: "assets/events/monaka.svg", alt: "もなかのイベント用・猫と花の仮イラスト" },
      "ちま": { src: "assets/events/chima.svg", alt: "ちまのイベント用・花束とリボンの仮イラスト" },
      "あらた": { src: "assets/events/arata.svg", alt: "あらたのイベント用・ティーカップの仮イラスト" },
      "あくび": { src: "assets/events/akubi.svg", alt: "あくびのイベント用・月と猫の仮イラスト" }
    },
    events: {}
  },

  // Display/matching correction only; the observed post and its raw names stay intact.
  observationNameCorrections: {
    "2096074325120237794": {
      "つぽみ": { name: "つぼみ", reason: "利用者確認（2026-09-06）" }
    }
  },

  // Reviewed shift interpretation for this source only; raw events remain intact.
  personalEventAdditions: {
    "2096253883677044837": {
      name: "ららこ",
      authorId: "2065375500131028992",
      authorScreenName: "rarako_zettai",
      date: "2026-09-06",
      events: [
        { shift: "夜", kind: "placement", storeId: "s2", excerpt: "1号店➡️2号店 / お昼1号店" }
      ],
      reason: "User-reviewed 12:00-22:00, day at s1 and current s1-to-s2 announcement (2026-09-06)"
    }
  },

  displayNames: {
    "まこっちゃん": "まこと"
  },

  // Keep the saved official first-service rank. Insert confirmed unpublished normals:
  // debuts.csv: yume 2026-04-17 < milestones.csv: cheru 2026-04-20;
  // debuts.csv: mochi 2026-06-04 < piano 2026-06-08. No observed-first-date inference.
  normalOrderBefore: {
    "ゆめ": "ちぇる",
    "もち": "ぴあの"
  },

  roster: [
    "ひかり",
    "あむ",
    "みりあ",
    "はぴる",
    "ちさと",
    "ねむり",
    "える",
    "あめる",
    "こい",
    "きらり",
    "すくい",
    "ひなり",
    "のの",
    "しゃち",
    "あくび",
    "うな",
    "こえび",
    "もなか",
    "かなた",
    "まひろ",
    "るるか",
    "こん",
    "えみ",
    "つぼみ",
    "みえる",
    "ちょこ",
    "ちゆ",
    "ららこ",
    "ちま",
    "ちぇる",
    "いと",
    "ゆめ",
    "にゃな",
    "ぴあの",
    "もち",
    "まこっちゃん",
    "あらた",
    "うる",
    "みりん",
    "けだま"
  ],

  kitchenStaff: [
    "まこっちゃん",
    "あらた",
    "うる",
    "みりん",
    "けだま"
  ],

  // 公式サイトのメイドさん紹介にまだ載っていない人。載っていないだけで在籍していて、
  // 予定も出しています。配属はお店からの案内で分かるので homeStore には入れますが、
  // 公式サイトと突き合わせて確かめられないので、ここに名前を残しておきます。
  // サイトに載ったら配属を照合して、ここから消してください。
  unpostedMaids: ["ゆめ", "にゃな", "ぴあの", "もち"],

  // 見習いを終えてノーマルになった日。この日より前は見習いなので、予定表には載らず
  // 「当日にならないと分からない人」でした。店舗数の判定はその日の予定表に何人載るかで
  // 決めるため、過去の人数を数えるときも昇格前の出勤は数えません。数えてしまうと
  // 「事前に分かっていた人数」が実際より多く見え、店舗数の閾値が上にずれます。
  //
  // 日付は公式X（`*_zettai`）の開設日です。ノーマルになるとアカウントをもらうので、
  // 開設日が昇格日のかわりになります。正確な告知日が分かったら直してください。
  promotedAt: {
    "ゆめ": "2026-07-18",
    "にゃな": "2026-08-04",
    "ぴあの": "2026-08-21",
    "もち": "2026-08-25"
  },

  // 所属店舗。公式サイトのメイドさん紹介（店舗タブ `shopNum01`〜`04` が `s1`〜`s4`）が
  // 出典ですが、まだ載っていない人（`unpostedMaids`）はお店からの案内によります。
  homeStore: {
    "ひかり": "s1",
    "あむ": "s3",
    "みりあ": "s4",
    "はぴる": "s3",
    "ちさと": "s1",
    "ねむり": "s4",
    "える": "s4",
    "あめる": "s3",
    "こい": "s3",
    "きらり": "s1",
    "すくい": "s4",
    "ひなり": "s3",
    "のの": "s2",
    "しゃち": "s1",
    "あくび": "s2",
    "うな": "s1",
    "こえび": "s4",
    "もなか": "s1",
    "かなた": "s2",
    "まひろ": "s2",
    "るるか": "s4",
    "こん": "s1",
    "えみ": "s1",
    "つぼみ": "s1",
    "みえる": "s4",
    "ちょこ": "s2",
    "ちゆ": "s1",
    "ららこ": "s2",
    "ちま": "s3",
    "ちぇる": "s3",
    "いと": "s2",
    "ゆめ": "s2",
    "にゃな": "s1",
    "ぴあの": "s2",
    "もち": "s2",
    "まこっちゃん": "s2",
    "あらた": "s3",
    "うる": "s3",
    "みりん": "s1",
    "けだま": "s1"
  },

  schedule: {
    "2026-09-01": {
      "昼": [
        { name: "みりあ" },
        { name: "きらり" },
        { name: "すくい" },
        { name: "えみ" },
        { name: "ちゆ" },
        { name: "あらた" },
        { name: "けだま" }
      ],
      "夜": [
        { name: "あむ" },
        { name: "みりあ" },
        { name: "える" },
        { name: "あくび" },
        { name: "こえび" },
        { name: "もなか", featured: true, eventLabel: "1周年" },
        { name: "まこっちゃん" },
        { name: "あらた" }
      ]
    },
    "2026-09-02": {
      "昼": [
        { name: "ひかり" },
        { name: "ちさと" },
        { name: "ねむり" },
        { name: "あめる" },
        { name: "しゃち" },
        { name: "かなた" },
        { name: "るるか" },
        { name: "あらた" },
        { name: "けだま" }
      ],
      "夜": [
        { name: "ひかり" },
        { name: "あめる" },
        { name: "うな" },
        { name: "こえび" },
        { name: "ぴあの" }
      ]
    },
    "2026-09-03": {
      "昼": [
        { name: "あむ" },
        { name: "みりあ" },
        { name: "ねむり" },
        { name: "こい" },
        { name: "ひなり" },
        { name: "しゃち" },
        { name: "もなか" },
        { name: "るるか" },
        { name: "こん" },
        { name: "ちょこ" },
        { name: "ちゆ" },
        { name: "ちま", featured: true, eventLabel: "生誕" },
        { name: "ちぇる" },
        { name: "あらた" }
      ],
      "夜": [
        { name: "ひかり" },
        { name: "あむ" },
        { name: "みりあ" },
        { name: "ひなり" },
        { name: "もなか" },
        { name: "つぼみ" },
        { name: "みえる" },
        { name: "ららこ" },
        { name: "あらた" }
      ]
    },
    "2026-09-04": {
      "昼": [
        { name: "える" },
        { name: "あめる" },
        { name: "こい" },
        { name: "もなか" },
        { name: "かなた" },
        { name: "るるか" },
        { name: "こん" }
      ],
      "夜": [
        { name: "あむ" },
        { name: "あめる" },
        { name: "うな" },
        { name: "るるか" },
        { name: "みえる" },
        { name: "ちゆ" },
        { name: "ぴあの" }
      ]
    },
    "2026-09-05": {
      "昼": [
        { name: "みりあ" },
        { name: "ちさと" },
        { name: "こい" },
        { name: "ひなり" },
        { name: "しゃち" },
        { name: "うな" },
        { name: "かなた" },
        { name: "つぼみ" },
        { name: "ちま" },
        { name: "ちぇる" },
        { name: "あらた" }
      ],
      "夜": [
        { name: "あむ" },
        { name: "みりあ" },
        { name: "はぴる" },
        { name: "あめる" },
        { name: "しゃち" },
        { name: "みえる" },
        { name: "ちゆ" },
        { name: "ららこ" },
        { name: "あらた" },
        { name: "けだま" }
      ]
    },
    "2026-09-06": {
      "昼": [
        { name: "あむ" },
        { name: "みりあ" },
        { name: "あくび" },
        { name: "もなか" },
        { name: "るるか" },
        { name: "こん" },
        { name: "つぼみ" },
        { name: "ちょこ" },
        { name: "ららこ" },
        { name: "ちぇる" },
        { name: "ぴあの" },
        { name: "あらた" }
      ],
      "夜": [
        { name: "あむ" },
        { name: "あめる" },
        { name: "きらり" },
        { name: "ひなり" },
        { name: "しゃち" },
        { name: "こえび" },
        { name: "るるか" },
        { name: "ららこ" },
        { name: "ぴあの" },
        { name: "あらた" }
      ]
    },
    "2026-09-07": {
      "昼": [
        { name: "みりあ" },
        { name: "こい" },
        { name: "うな" },
        { name: "もなか" },
        { name: "かなた" },
        { name: "ちゆ" },
        { name: "ぴあの" },
        { name: "けだま" }
      ],
      "夜": [
        { name: "ひなり" },
        { name: "こえび" },
        { name: "もなか" },
        { name: "みえる" },
        { name: "ちょこ" },
        { name: "ちま" }
      ]
    },
    "2026-09-08": {
      "昼": [
        { name: "ひかり" },
        { name: "あむ" },
        { name: "みりあ" },
        { name: "ちさと" },
        { name: "こい" },
        { name: "もなか" },
        { name: "かなた" },
        { name: "るるか" },
        { name: "ちゆ" },
        { name: "あらた", featured: true, eventLabel: "7周年" },
        { name: "けだま" }
      ],
      "夜": [
        { name: "あむ" },
        { name: "みりあ" },
        { name: "える" },
        { name: "あめる" },
        { name: "うな" },
        { name: "もなか" },
        { name: "かなた" },
        { name: "るるか" },
        { name: "ぴあの" },
        { name: "あらた", featured: true, eventLabel: "7周年" }
      ]
    },
    "2026-09-09": {
      "昼": [
        { name: "ねむり" },
        { name: "あめる" },
        { name: "こい" },
        { name: "きらり" },
        { name: "ひなり" },
        { name: "ちま" },
        { name: "あらた" }
      ],
      "夜": [
        { name: "ちさと" },
        { name: "かなた" },
        { name: "ちゆ" },
        { name: "ららこ" }
      ]
    },
    "2026-09-10": {
      "昼": [
        { name: "みりあ" },
        { name: "ねむり" },
        { name: "きらり" },
        { name: "ひなり" },
        { name: "もなか" },
        { name: "るるか" },
        { name: "こん" },
        { name: "ちぇる" },
        { name: "あらた" },
        { name: "けだま" }
      ],
      "夜": [
        { name: "みりあ" },
        { name: "ちさと" },
        { name: "もなか" },
        { name: "るるか" },
        { name: "ちょこ" },
        { name: "ちま" },
        { name: "ぴあの" },
        { name: "あらた" }
      ]
    },
    "2026-09-11": {
      "昼": [
        { name: "みりあ" },
        { name: "こい" },
        { name: "ひなり" },
        { name: "もなか" },
        { name: "かなた" },
        { name: "ちゆ" }
      ],
      "夜": [
        { name: "ひかり" },
        { name: "あめる" },
        { name: "うな" },
        { name: "こえび" },
        { name: "もなか" },
        { name: "かなた" },
        { name: "みえる" },
        { name: "ちゆ" }
      ]
    },
    "2026-09-12": {
      "昼": [
        { name: "ひかり" },
        { name: "みりあ" },
        { name: "こい" },
        { name: "かなた" },
        { name: "るるか" },
        { name: "こん" },
        { name: "ららこ" },
        { name: "けだま" }
      ],
      "夜": [
        { name: "みりあ" },
        { name: "あめる" },
        { name: "きらり" },
        { name: "るるか" },
        { name: "みえる" },
        { name: "ちゆ" },
        { name: "ららこ" },
        { name: "ちま" },
        { name: "ぴあの" }
      ]
    },
    "2026-09-13": {
      "昼": [
        { name: "みりあ" },
        { name: "あめる" },
        { name: "すくい" },
        { name: "ひなり" },
        { name: "うな" },
        { name: "こん" },
        { name: "ちょこ" },
        { name: "ららこ" },
        { name: "ちま" },
        { name: "ちぇる" },
        { name: "あらた" }
      ],
      "夜": [
        { name: "あむ" },
        { name: "ねむり" },
        { name: "あめる" },
        { name: "きらり" },
        { name: "あくび", featured: true, eventLabel: "生誕" },
        { name: "かなた" },
        { name: "ちゆ" },
        { name: "ぴあの" }
      ]
    },
    "2026-09-14": {
      "昼": [
        { name: "あむ" },
        { name: "こい" },
        { name: "しゃち" },
        { name: "うな" },
        { name: "こん" },
        { name: "けだま" }
      ],
      "夜": [
        { name: "ひかり" },
        { name: "あめる" },
        { name: "ひなり" },
        { name: "しゃち" },
        { name: "ちょこ" }
      ]
    },
    "2026-09-15": {
      "昼": [
        { name: "あむ" },
        { name: "みりあ" },
        { name: "ちさと" },
        { name: "ねむり" },
        { name: "える" },
        { name: "しゃち" },
        { name: "るるか" },
        { name: "ちま" },
        { name: "あらた" }
      ],
      "夜": [
        { name: "ひかり" },
        { name: "あむ" },
        { name: "うな" },
        { name: "こえび" },
        { name: "ちゆ" },
        { name: "ららこ" },
        { name: "あらた" }
      ]
    }
  }
};

// Reviewed original sources, not model inference or observed attendance.
window.SCHEDULE_DATA.sourceConfirmedPlans = [
  {"source":{"id":"2100220713793958154","url":"https://x.com/hikari_zettai/status/2100220713793958154","name":"ひかり","authorId":"834432421604962304","authorScreenName":"hikari_zettai","createdAt":"2026-09-16T13:50:13.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"7fcef91243fbd68a62c51121bf864c89c7dfc2afc499a21b3b5fcb70d3bb4d2b","imageSha256":[]}},"days":[{"date":"2026-09-16","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-18","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"},{"date":"2026-09-19","shifts":["夜"],"explicitStart":"16:00","explicitEnd":"22:00"},{"date":"2026-09-20","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-23","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"18:00"},{"date":"2026-09-27","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"},{"date":"2026-09-29","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"}]},
  {"source":{"id":"2098952024880758947","url":"https://x.com/amu_zettai/status/2098952024880758947","name":"あむ","authorId":"1180156105181159424","authorScreenName":"amu_zettai","createdAt":"2026-09-13T01:48:54.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"586ebfb5b255046e81765459f6690f1da4e57b24a39f6c1264923cf0696d4999","imageSha256":["2fecc9c2cea44ac930f877bc346bd0b36b633eec775fdd180a92425988fd63ea"]}},"days":[{"date":"2026-09-17","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-18","shifts":["夜"],"explicitStart":"16:00","explicitEnd":"22:00"},{"date":"2026-09-20","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-21","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-22","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"},{"date":"2026-09-24","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"},{"date":"2026-09-26","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"},{"date":"2026-09-27","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"}]},
  {"source":{"id":"2099067304961257740","url":"https://x.com/miria_zettai/status/2099067304961257740","name":"みりあ","authorId":"1484339322476118028","authorScreenName":"miria_zettai","createdAt":"2026-09-13T09:26:58.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"e4cd95c9fa97006a71683ac93c5296885a9ad5ae4ae254292bfcfd2e06ebb36f","imageSha256":["9c6a512cd29583a8633432627e7581eab1d83f266f091ee7582be8a87876007c","fe21c1dd3d91dec0544b981c73b18ab928886bb912e369860e91177abcb3dce8"]}},"days":[{"date":"2026-09-17","shifts":["昼","夜"]},{"date":"2026-09-18","shifts":["昼"]},{"date":"2026-09-19","shifts":["昼","夜"]},{"date":"2026-09-20","shifts":["夜"]},{"date":"2026-09-22","shifts":["昼","夜"]},{"date":"2026-09-23","shifts":["昼"]},{"date":"2026-09-25","shifts":["昼"],"qualifier":"long","derivedHours":{"start":"12:00","end":"18:00","basis":"user-approved-qualifier-rule"}},{"date":"2026-09-26","shifts":["昼","夜"]},{"date":"2026-09-27","shifts":["昼"]},{"date":"2026-09-29","shifts":["昼"],"qualifier":"long","derivedHours":{"start":"12:00","end":"18:00","basis":"user-approved-qualifier-rule"}},{"date":"2026-09-30","shifts":["昼"]}]},
  {"source":{"id":"2099868407667839347","url":"https://x.com/hapiru_zettai/status/2099868407667839347","name":"はぴる","authorId":"1555189951083737091","authorScreenName":"hapiru_zettai","createdAt":"2026-09-15T14:30:16.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"b36f58cd1477ffd0441ff26c0fc4eda9e53e16685c875d84fdcf46338cf8120b","imageSha256":["6d1c37cf85349a7330d7e957e75f9a6dc8f473ba126a7b92f403460ad0f42531"]}},"days":[{"date":"2026-09-16","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-18","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-19","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-22","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"},{"date":"2026-09-25","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"},{"date":"2026-09-30","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"}]},
  {"source":{"id":"2098982452190646622","url":"https://x.com/chisato_zettai/status/2098982452190646622","name":"ちさと","authorId":"1650367235700174848","authorScreenName":"chisato_zettai","createdAt":"2026-09-13T03:49:48.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"d72432656f0b5e3db8802a1272da3082f891eb20dbae4af0ee1551b125f9daf7","imageSha256":["191e0fa4f5034213620abe823f2889cbab6761ce4f220fe0e91d1eb11d722cba","59fbe1c00430aa3380b4e25652f9574fdafe2b1ac63bf23be90f673ca9d84fea"]}},"days":[{"date":"2026-09-18","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-19","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-23","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-25","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-26","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-27","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-29","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"}]},
  {"source":{"id":"2098792049260793868","url":"https://x.com/nemuri_zettai/status/2098792049260793868","name":"ねむり","authorId":"1709489909621571584","authorScreenName":"nemuri_zettai","createdAt":"2026-09-12T15:13:12.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"11925b86087fbd5d16c29166c85528a0ab93c49c107ce3e9f631f9ccd8930e17","imageSha256":["55e6632de27c7b41435524370ef87e494e5ed0874eb3637c052192bda37fb5f5","0845e2cf50df56b8aa0f6c69cfc85621d13bfb026da17ee6b216d9fe81af450a"]}},"days":[{"date":"2026-09-16","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-17","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-19","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-25","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"18:00","qualifier":"long"},{"date":"2026-09-26","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"18:00","qualifier":"long"},{"date":"2026-09-29","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-30","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"}]},
  {"source":{"id":"2100527053791731979","url":"https://x.com/eru_zettai/status/2100527053791731979","name":"える","authorId":"1823541746787332096","authorScreenName":"eru_zettai","createdAt":"2026-09-17T10:07:30.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"7a66a7879e8ae3b0c3535bc13226fa6586d060c4c84af71e7d79ae668887c40e","imageSha256":["c3c13608844fea5fd168afe0400da35391f14835a11c6cf20825fcaff321e26d"]}},"days":[{"date":"2026-09-18","shifts":["夜"]},{"date":"2026-09-19","shifts":["夜"]},{"date":"2026-09-23","shifts":["昼"]}]},
  {"source":{"id":"2099156926835896598","url":"https://x.com/ameru_zettai/status/2099156926835896598","name":"あめる","authorId":"1822822097141575680","authorScreenName":"ameru_zettai","createdAt":"2026-09-13T15:23:06.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"cb2b75c37abfeffbe7c9afd0567985eed40a2276631b8709472226cb3b2e9a6d","imageSha256":["c5c812cb8f4ba293eea4af7e326eda2e20aca666c2101b2533ac86158a4238f8"]}},"days":[{"date":"2026-09-17","shifts":["昼"]},{"date":"2026-09-18","shifts":["昼","夜"]},{"date":"2026-09-21","shifts":["夜"]},{"date":"2026-09-22","shifts":["夜"]},{"date":"2026-09-23","shifts":["昼"]},{"date":"2026-09-25","shifts":["昼"],"qualifier":"long","derivedHours":{"start":"12:00","end":"18:00","basis":"user-approved-qualifier-rule"}},{"date":"2026-09-26","shifts":["昼"]},{"date":"2026-09-27","shifts":["昼"]},{"date":"2026-09-29","shifts":["昼"]},{"date":"2026-09-30","shifts":["昼"]}]},
  {"source":{"id":"2098860615892971879","url":"https://x.com/koi_zettai/status/2098860615892971879","name":"こい","authorId":"1832320712545333248","authorScreenName":"koi_zettai","createdAt":"2026-09-12T19:45:40.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"deac0d5864fd7618bfc11c24d191a24b3963e7c01ef14b3da8477d8521cafc34","imageSha256":[]}},"days":[{"date":"2026-09-18","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"}]},
  {"source":{"id":"2099012871837659257","url":"https://x.com/kirari_zettai/status/2099012871837659257","name":"きらり","authorId":"1839927788377387011","authorScreenName":"kirari_zettai","createdAt":"2026-09-13T05:50:41.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"adc3d648b4662007042ab2e52907f7243333d24521d0a5aa9ea6c41da3eed6b2","imageSha256":["2972334fec53e4c2035de2f2cbe51a9c18016001bb26a8974cbed03178cc099b"]}},"days":[{"date":"2026-09-17","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-24","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-28","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"}]},
  {"source":{"id":"2098819209220493804","url":"https://x.com/sukui_zettai/status/2098819209220493804","name":"すくい","authorId":"1858869123947917314","authorScreenName":"sukui_zettai","createdAt":"2026-09-12T17:01:08.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"bf97946f41ce4c9d7b7b418a056baefc540dcae7117b14dd9373259879f02654","imageSha256":[]}},"days":[{"date":"2026-09-19","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"},{"date":"2026-09-20","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-21","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"},{"date":"2026-09-22","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-26","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00","reviewNote":"user-confirmed-numeric-date; printed weekday Wednesday conflicts with 2026-09-26 Saturday"},{"date":"2026-09-27","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00","reviewNote":"user-confirmed-numeric-date; printed weekday Thursday conflicts with Sunday"},{"date":"2026-09-29","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00","reviewNote":"user-confirmed-numeric-date; printed weekday Friday conflicts with Tuesday"}]},
  {"source":{"id":"2099141143233794272","url":"https://x.com/hinari_zettai/status/2099141143233794272","name":"ひなり","authorId":"1891056504125743104","authorScreenName":"hinari_zettai","createdAt":"2026-09-13T14:20:23.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"657110b07588aa7263e77b3f4cb676a4d3a43bc50470207da9ece83508e52c5a","imageSha256":["b6c4668cec3aad001c973d7aa4f7200ec10949b78e521b6d2664beeceeede7ff"]}},"days":[{"date":"2026-09-18","shifts":["昼","夜"]},{"date":"2026-09-19","shifts":["夜"]},{"date":"2026-09-21","shifts":["夜"]},{"date":"2026-09-22","shifts":["昼"]},{"date":"2026-09-24","shifts":["夜"]},{"date":"2026-09-25","shifts":["夜"]},{"date":"2026-09-26","shifts":["昼"]},{"date":"2026-09-27","shifts":["昼"]},{"date":"2026-09-28","shifts":["夜"],"unmappedQualifier":"ながめ夜"},{"date":"2026-09-30","shifts":["夜"]}]},
  {"source":{"id":"2101628334845403197","url":"https://x.com/nono_zettai/status/2101628334845403197","name":"のの","authorId":"1935204513381765120","authorScreenName":"nono_zettai","createdAt":"2026-09-20T11:03:36.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"75f262375ad35769a80a5921ac4ad61fd5e37f7bb403ac9aa3e52ca7c2fcc24b","imageSha256":["a05c312efda5b6d7d0b57f23a37f1ef46a1fdcb087c4d48dc6b0113d65d25069"]}},"days":[{"date":"2026-09-22","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-24","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-26","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-28","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-29","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"}]},
  {"source":{"id":"2098798188904038870","url":"https://x.com/shachi_zettai/status/2098798188904038870","name":"しゃち","authorId":"1937381891445075968","authorScreenName":"shachi_zettai","createdAt":"2026-09-12T15:37:36.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"db3f23def4d558f84c0ca59e58e1cc0dd2b3fa424f8f30f703a19837e5d6dfe0","imageSha256":["18d4faf1ab8dcfc12b9ba98d7c751c7cd0429c260a90c100f6e89d4e3df3b125"]}},"days":[{"date":"2026-09-16","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-19","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-24","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"18:00"},{"date":"2026-09-26","shifts":["夜"],"explicitStart":"16:00","explicitEnd":"22:00"},{"date":"2026-09-27","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-29","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-30","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"}]},
  {"source":{"id":"2098804909068148975","url":"https://x.com/akubi_zettai/status/2098804909068148975","name":"あくび","authorId":"1954467457902809088","authorScreenName":"akubi_zettai","createdAt":"2026-09-12T16:04:18.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"70f61f4cc923fd0e46dc8a6b9522d711cbb823b702fc563168621257102f9e8f","imageSha256":["7398cae80a33e118172b9a299e878575603f4e0728dd1e210406c4dd09278142"]}},"days":[{"date":"2026-09-18","shifts":["夜"]},{"date":"2026-09-19","shifts":["夜"]},{"date":"2026-09-22","shifts":["夜"]},{"date":"2026-09-24","shifts":["夜"]},{"date":"2026-09-25","shifts":["夜"]},{"date":"2026-09-29","shifts":["夜"]}]},
  {"source":{"id":"2098788911334240369","url":"https://x.com/una_zettai/status/2098788911334240369","name":"うな","authorId":"1959230080657563648","authorScreenName":"una_zettai","createdAt":"2026-09-12T15:00:44.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"74f0f1fdda65d3e9e4605aa2042d95ccd843319ce008346cf7610dc60817c97b","imageSha256":["39b1f8814b3151a5e48c84c9a788a97e6ac1dbb27bbe2c41ada91df81ceacaa0"]}},"days":[{"date":"2026-09-18","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-19","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-20","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-23","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-28","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-29","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"}]},
  {"source":{"id":"2099877928536551726","url":"https://x.com/koebi_zettai/status/2099877928536551726","name":"こえび","authorId":"1966141137833664513","authorScreenName":"koebi_zettai","createdAt":"2026-09-15T15:08:06.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"6faf659ca9d569688dfeb80101bc8082875bb14f72ffbe39592e921f96b98d05","imageSha256":["f6cac5e646ad654332559e0b94ab04e0ad790fef3117b50a9b683515175af478","bcbd60f42d4e814cc4dd59be57595ee7ebb4c22ba13a7cba070cae5538807d6d"]}},"days":[{"date":"2026-09-21","shifts":["夜"]},{"date":"2026-09-23","shifts":["昼"]},{"date":"2026-09-26","shifts":["夜"]}]},
  {"source":{"id":"2099141960229622271","url":"https://x.com/monaka1_zettai/status/2099141960229622271","name":"もなか","authorId":"2010583256014729217","authorScreenName":"monaka1_zettai","createdAt":"2026-09-13T14:23:38.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"91c26f3c49909396bf8252b74c1b177c3ac169a52b55f1fd0c2a6088ba54015d","imageSha256":[]}},"days":[{"date":"2026-09-17","shifts":["昼","夜"]},{"date":"2026-09-18","shifts":["昼"]},{"date":"2026-09-19","shifts":["昼"]},{"date":"2026-09-20","shifts":["昼","夜"]},{"date":"2026-09-24","shifts":["昼","夜"]},{"date":"2026-09-25","shifts":["昼","夜"]}]},
  {"source":{"id":"2099142246499340587","url":"https://x.com/monaka1_zettai/status/2099142246499340587","name":"もなか","authorId":"2010583256014729217","authorScreenName":"monaka1_zettai","createdAt":"2026-09-13T14:24:46.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"d71716f0b5e2407cb0692cf7cc36c3338128211b319908a870f432c00092fbfe","imageSha256":[],"replyTo":"2099141960229622271"}},"days":[{"date":"2026-09-26","shifts":["昼"],"explicitEnd":"18:00"},{"date":"2026-09-28","shifts":["昼","夜"]}]},
  {"source":{"id":"2099317010735956211","url":"https://x.com/kanata2_zettai/status/2099317010735956211","name":"かなた","authorId":"1988584542077370368","authorScreenName":"kanata2_zettai","createdAt":"2026-09-14T01:59:13.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"4d8faccba77551b3bab1164c29f86b991abbd5e68bf701e1b151de4337bff64e","imageSha256":["df98c32926331f3bbb9a1ed30b4086536f259be5821803b66d884231b2453356"]}},"days":[{"date":"2026-09-19","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-20","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-21","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-23","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-26","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"},{"date":"2026-09-27","shifts":["昼"],"explicitStart":"11:00","explicitEnd":"18:00","qualifier":"long"},{"date":"2026-09-29","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-30","shifts":["夜"],"explicitStart":"17:30","explicitEnd":"22:00","qualifier":"late"}]},
  {"source":{"id":"2098802016588075431","url":"https://x.com/mahiro1_zettai/status/2098802016588075431","name":"まひろ","authorId":"1991081247444258817","authorScreenName":"mahiro1_zettai","createdAt":"2026-09-12T15:52:49.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"ccfdbdee76d94079958634ecbe59984aa7ce3fc5c5586d2329c13c4ff30a9071","imageSha256":["b4ace558b0316b7aef03cf59b2429bc99ee4242ff3eeb1c4cd2f5e3f016ec3bb"]}},"days":[{"date":"2026-09-17","shifts":["夜"]},{"date":"2026-09-19","shifts":["夜"]},{"date":"2026-09-23","shifts":["夜"]},{"date":"2026-09-27","shifts":["夜"]}]},
  {"source":{"id":"2099501210298646698","url":"https://x.com/ruruka_zettai/status/2099501210298646698","name":"るるか","authorId":"2017568855321964544","authorScreenName":"ruruka_zettai","createdAt":"2026-09-14T14:11:10.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"8ef3eecaab1f9fed79e11d99193905d7a177c98904fbea8122501f3c97ed1620","imageSha256":["946c8995b3d16e7ddf90e94260b2da78d6fb5c4779ffe71b26055a613a0ad127"]}},"days":[{"date":"2026-09-16","shifts":["昼"]},{"date":"2026-09-17","shifts":["昼"]},{"date":"2026-09-18","shifts":["昼"],"qualifier":"long","derivedHours":{"start":"12:00","end":"18:00","basis":"user-approved-qualifier-rule"}},{"date":"2026-09-20","shifts":["昼","夜"]},{"date":"2026-09-21","shifts":["夜"]},{"date":"2026-09-23","shifts":["昼","夜"]},{"date":"2026-09-25","shifts":["昼","夜"]},{"date":"2026-09-27","shifts":["昼","夜"]},{"date":"2026-09-29","shifts":["昼"]},{"date":"2026-09-30","shifts":["夜"]}]},
  {"source":{"id":"2098789529507561834","url":"https://x.com/kon1_zettai/status/2098789529507561834","name":"こん","authorId":"2025115850811080707","authorScreenName":"kon1_zettai","createdAt":"2026-09-12T15:03:12.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"782f4d3e0e415db5d0679048a2ec5171df611f873db0fece990f6b66018d73ed","imageSha256":["2110688a4070bac0e6f791d3128ddf7ddb0b58081b4121a43c8a2e8dcb1ec2c6"]}},"days":[{"date":"2026-09-16","shifts":["夜"],"explicitStart":"17:30","explicitEnd":"22:00"},{"date":"2026-09-18","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-19","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-20","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-23","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-27","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-28","shifts":["夜"],"explicitStart":"17:30","explicitEnd":"22:00"},{"date":"2026-09-30","shifts":["夜"],"explicitStart":"17:30","explicitEnd":"22:00"}]},
  {"source":{"id":"2101675178933862462","url":"https://x.com/emi_zettai/status/2101675178933862462","name":"えみ","authorId":"2034096297956007936","authorScreenName":"emi_zettai","createdAt":"2026-09-20T14:09:44.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"d9889e6dd26d9c5ea2e770e92c6564928d3d6d653c3a050a22f8c1e845e7f805","imageSha256":["7be221a633b8ca2674c5e039dc28442550edf6ab99ce34275ebb8cf23f438ae2"]}},"days":[{"date":"2026-09-21","shifts":["昼"]},{"date":"2026-09-22","shifts":["昼"]},{"date":"2026-09-23","shifts":["昼"]},{"date":"2026-09-26","shifts":["昼"]}]},
  {"source":{"id":"2098789893166223615","url":"https://x.com/tsubomi_zettai/status/2098789893166223615","name":"つぼみ","authorId":"2033429419910639616","authorScreenName":"tsubomi_zettai","createdAt":"2026-09-12T15:04:38.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"011a64f18981276cb069cff4b00fa206e9a707e80dce85fc435b3e59ffa9f531","imageSha256":["48848d000173b1524e61f2cda927b5b8b8716786fe72aa4dbd0efd45969d202c"]}},"days":[{"date":"2026-09-20","shifts":["昼","夜"]},{"date":"2026-09-21","shifts":["昼"]},{"date":"2026-09-23","shifts":["昼"]},{"date":"2026-09-24","shifts":["夜"]},{"date":"2026-09-29","shifts":["昼"]}]},
  {"source":{"id":"2098789219745534016","url":"https://x.com/mieru_zettai/status/2098789219745534016","name":"みえる","authorId":"2031990697998630913","authorScreenName":"mieru_zettai","createdAt":"2026-09-12T15:01:58.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"43baa4cf9bc529bcbfaa4de335448ea7507ee9625fd7f1f55e476587c6af21c3","imageSha256":["6f2bf3ed03373463d40b3c709ff34e81ede4b7df1aa20a2109d7f9d1a801c007","8d2c043cb7e42f41671b28aa3e00c5d06f76573a7e679a14fd4d51ed890dc479"]}},"days":[{"date":"2026-09-16","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-20","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-30","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"}]},
  {"source":{"id":"2098790051366998244","url":"https://x.com/choko_zettai/status/2098790051366998244","name":"ちょこ","authorId":"2041767360751669248","authorScreenName":"choko_zettai","createdAt":"2026-09-12T15:05:16.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"d22182328a44fa008329efa8ad2b2940d9dc32eafb93227f9de77d6bd480b54f","imageSha256":["0b8d5fee022a2e6e4f657df0edc3d9e33f0eb50baf521453b10220f998c7a14f"]}},"days":[{"date":"2026-09-20","shifts":["昼"]},{"date":"2026-09-21","shifts":["昼"]},{"date":"2026-09-24","shifts":["夜"]},{"date":"2026-09-27","shifts":["夜"]},{"date":"2026-09-29","shifts":["昼"]}]},
  {"source":{"id":"2098837865300189236","url":"https://x.com/chiyu_zettai/status/2098837865300189236","name":"ちゆ","authorId":"2055533720107454464","authorScreenName":"chiyu_zettai","createdAt":"2026-09-12T18:15:16.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"b9d5285f127949f5d8433f684cdabf2cca2b5bc2e8abde82a6bc7133370937cc","imageSha256":["f9ebd72a3cb2f7aa1cace54eb3ddcac4eb982d0e23c6008f4ec32c130fbe7961"]}},"days":[{"date":"2026-09-16","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-18","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-19","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-21","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-22","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-23","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-25","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-26","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"},{"date":"2026-09-28","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-29","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"}]},
  {"source":{"id":"2099534635592208850","url":"https://x.com/rarako_zettai/status/2099534635592208850","name":"ららこ","authorId":"2065375500131028992","authorScreenName":"rarako_zettai","createdAt":"2026-09-14T16:23:59.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"c04111666210041f62d05415bd83b67d023f7017188467894235fdc832022f3b","imageSha256":["55a83655f1df4967cf12ac33b2d640ff7b20c627b14723cec91dfadebc56f930","05e8970b2b4cd96a5a6bb3857734a8c5afefb4ca574fcf1b5565cd7b9109d0a7"]}},"days":[{"date":"2026-09-17","shifts":["夜"],"explicitStart":"17:30","explicitEnd":"22:00"},{"date":"2026-09-20","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-23","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-24","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-25","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-26","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"}]},
  {"source":{"id":"2098789438486954247","url":"https://x.com/cheru2_zettai/status/2098789438486954247","name":"ちぇる","authorId":"2075784010819928064","authorScreenName":"cheru2_zettai","createdAt":"2026-09-12T15:02:50.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"016ef418057e080479b0d76d2f559688e2786bc1699ed6a827de5a104f906906","imageSha256":["2c551ba3a6517478df99d89da49151fb671ad623b215378d865ca59546f2703e"]}},"days":[{"date":"2026-09-17","shifts":["夜"]},{"date":"2026-09-19","shifts":["昼"]},{"date":"2026-09-21","shifts":["昼","夜"]},{"date":"2026-09-26","shifts":["昼"]},{"date":"2026-09-27","shifts":["昼"]}]},
  {"source":{"id":"2099142891172188649","url":"https://x.com/ito_zettai/status/2099142891172188649","name":"いと","authorId":"2080944098043977728","authorScreenName":"ito_zettai","createdAt":"2026-09-13T14:27:20.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"6fb65ab95c061a139598eedfb44aeb3fb20c7d5637e20682fea34c19dc528a04","imageSha256":["217a2766513ed2197f0874c6006b29c618d0f62576caffb27a8883287e091b19"]}},"days":[{"date":"2026-09-17","shifts":["夜"]},{"date":"2026-09-19","shifts":["昼"]},{"date":"2026-09-22","shifts":["昼","夜"]},{"date":"2026-09-26","shifts":["夜"],"qualifier":"early","derivedHours":{"start":"16:00","end":"22:00","basis":"user-approved-qualifier-rule"}},{"date":"2026-09-28","shifts":["夜"]},{"date":"2026-09-30","shifts":["夜"]}]},
  {"source":{"id":"2099146543093936376","url":"https://x.com/yume1_zettai/status/2099146543093936376","name":"ゆめ","authorId":"2078348082459373569","authorScreenName":"yume1_zettai","createdAt":"2026-09-13T14:41:50.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"8e4354f6531ea9c22030784801e723bd1c396c37e977cf92af01cb583059cefd","imageSha256":[]}},"days":[{"date":"2026-09-16","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-19","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-24","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-25","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-28","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"}]},
  {"source":{"id":"2098794737998483839","url":"https://x.com/nyana_zettai/status/2098794737998483839","name":"にゃな","authorId":"2084466877821329408","authorScreenName":"nyana_zettai","createdAt":"2026-09-12T15:23:53.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"593bbe9d6bf27804fd98c62b9a75058548672b6168935ab77ca4938f888c97e7","imageSha256":["30d68dbf8402ff99535bdf1df3f9b2a489aae23f88c0b3de464a5e10a14a4c88"]}},"days":[{"date":"2026-09-17","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-18","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-19","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-21","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-22","shifts":["昼","夜"],"explicitStart":"12:00","explicitEnd":"22:00"},{"date":"2026-09-26","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-27","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"}]},
  {"source":{"id":"2099037112658198776","url":"https://x.com/piano1_zettai/status/2099037112658198776","name":"ぴあの","authorId":"2090672918976221185","authorScreenName":"piano1_zettai","createdAt":"2026-09-13T07:27:00.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"c90ae9dc0ea2145be4134b5ae294f73640b846d7e7ed128dd7f65b1338e812df","imageSha256":["ce3ed94bbeb11fc1f1bd8768d018ee66b0e824028f50340ab164615806ebc217","e79c44a07c99d22607be65bd1c2bcb4e9d4c4603d80c68d3677309b6777885d1"]}},"days":[{"date":"2026-09-19","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-20","shifts":["夜"],"explicitStart":"16:00","explicitEnd":"22:00","qualifier":"early"},{"date":"2026-09-23","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-25","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-27","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-28","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"},{"date":"2026-09-30","shifts":["夜"],"explicitStart":"17:00","explicitEnd":"22:00"}]},
  {"source":{"id":"2098798148886184161","url":"https://x.com/mochi1_zettai/status/2098798148886184161","name":"もち","authorId":"2092131432810569728","authorScreenName":"mochi1_zettai","createdAt":"2026-09-12T15:37:27.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"c8db44063721336b505801022b98512b6fbd8a168dcbb0c13ee031d9a2e82691","imageSha256":["eeb8170b9cb55948f6241e24548fa11faa0ae1827e62b526fafee0fe58bdbde2"]}},"days":[{"date":"2026-09-19","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-23","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-26","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"},{"date":"2026-09-28","shifts":["昼"],"explicitStart":"12:00","explicitEnd":"17:00"}]},
  {"source":{"id":"2100163390434160756","url":"https://x.com/arata_zettai/status/2100163390434160756","name":"あらた","authorId":"1404766254552944645","authorScreenName":"arata_zettai","createdAt":"2026-09-16T10:02:26.000Z","sourceKind":"half-month-schedule","period":{"from":"2026-09-16","to":"2026-09-30"},"confirmation":{"method":"source-confirmed","reviewedAt":"2026-09-22T18:09:08.733356Z","bodySha256":"ce61f235ccf7e66be53d6159fbd976d597ff34e34cd5fcc2e94ecb89b555a692","imageSha256":["83c7f79702f619a450273dbbb7acd83d85e048c20dfda92ba97dd87d35c21da4","97c5f37580d09eeab24a6d298e0d5811bf0fe1c147ad17a1245a8246ffdcee6b"]}},"days":[{"date":"2026-09-18","shifts":["昼","夜"]},{"date":"2026-09-19","shifts":["昼"]},{"date":"2026-09-20","shifts":["昼","夜"]},{"date":"2026-09-23","shifts":["昼"]},{"date":"2026-09-24","shifts":["昼","夜"]},{"date":"2026-09-26","shifts":["昼","夜"]},{"date":"2026-09-27","shifts":["昼"]},{"date":"2026-09-29","shifts":["昼","夜"]},{"date":"2026-09-30","shifts":["昼"]}]}
];

{
  const data = window.SCHEDULE_DATA;
  data.lastUpdated = "2026年9月23日 原典確認分（9月後半35名）";
  const sourceFields = ["id", "url", "name", "authorId", "authorScreenName", "createdAt", "sourceKind"];
  const qualifierBoundary = { long: ["昼", "end"], early: ["夜", "start"], late: ["夜", "start"] };
  for (const plan of data.sourceConfirmedPlans) {
    const source = Object.fromEntries(sourceFields.map((key) => [key, plan.source[key]]));
    for (const day of plan.days) {
      const schedule = data.schedule[day.date] ??= { "昼": [], "夜": [] };
      for (const shift of day.shifts) {
        const entries = schedule[shift];
        let entry = entries.find((item) => item.name === source.name);
        if (!entry) {
          entry = { name: source.name };
          entries.push(entry);
        }
        entry.halfMonthSources ??= [];
        if (!entry.halfMonthSources.some((item) => item.id === source.id)) {
          entry.halfMonthSources.push(plan.source);
        }
        const facts = [];
        for (const boundary of ["start", "end"]) {
          const edge = boundary === "start" ? day.shifts[0] : day.shifts[day.shifts.length - 1];
          const explicitTime = shift === edge ? day[boundary === "start" ? "explicitStart" : "explicitEnd"] : null;
          const qualifier = qualifierBoundary[day.qualifier]?.[0] === shift &&
            qualifierBoundary[day.qualifier]?.[1] === boundary ? day.qualifier : null;
          if (explicitTime || qualifier) facts.push({
            serviceDate: day.date, shift, boundary, status: "set",
            qualifier, explicitTime: explicitTime ?? null, source
          });
        }
        if (facts.length) {
          entry.workTiming ??= { schemaVersion: 1, facts: [] };
          for (const fact of facts) {
            if (!entry.workTiming.facts.some((old) => old.source.id === source.id &&
                old.serviceDate === fact.serviceDate && old.shift === shift && old.boundary === fact.boundary)) {
              entry.workTiming.facts.push(fact);
            }
          }
        }
        entries.sort((a, b) => data.roster.indexOf(a.name) - data.roster.indexOf(b.name));
      }
    }
  }
}
