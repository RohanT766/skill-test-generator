CREATE TABLE "test_table" (
	"id" text PRIMARY KEY NOT NULL,
	"title" text NOT NULL,
	"price_cents" integer NOT NULL,
	"image_url" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
