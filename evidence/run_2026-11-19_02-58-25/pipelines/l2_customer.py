from pyspark.sql import SparkSession, functions as F
from pyspark.sql.types import StructType, StructField, StringType, IntegerType, TimestampType

# Define the schema for the target table
customer_schema = StructType([
    StructField("customer_id", IntegerType(), True),
    StructField("first_name", StringType(), True),
    StructField("last_name", StringType(), True),
    StructField("email", StringType(), True),
    StructField("phone", StringType(), True),
    StructField("city", StringType(), True),
    StructField("state_province", StringType(), True),
    StructField("country_code", StringType(), True),
    StructField("created_date", TimestampType(), True),
    StructField("modified_date", TimestampType(), True)
])

def build_customer_l2(df):
    # Cast customer_id to int
    df = df.withColumn("customer_id", F.col("customer_id").cast("int"))
    
    # Trim first_name and last_name
    df = df.withColumn("first_name", F.trim(F.col("first_name")))
    df = df.withColumn("last_name", F.trim(F.col("last_name")))
    
    # Convert email to lower case
    df = df.withColumn("email", F.lower(F.col("email")))
    
    # Direct transformations for phone, country_code, created_date, and modified_date
    df = df.withColumn("phone", F.col("phone"))
    df = df.withColumn("country_code", F.col("country_code"))
    df = df.withColumn("created_date", F.col("created_date"))
    df = df.withColumn("modified_date", F.col("modified_date"))
    
    # Trim city and state_province
    df = df.withColumn("city", F.trim(F.col("city")))
    df = df.withColumn("state_province", F.trim(F.col("state_province")))
    
    return df

def load():
    # Read from the source table
    df = spark.read.format("delta").load("dea_accelerator.bronze.customer")
    
    # Apply the transformation
    df = build_customer_l2(df)
    
    # Add audit columns
    df = df.withColumn("ingestion_timestamp", F.current_timestamp())
    df = df.withColumn("source_file", F.input_file_name())
    df = df.withColumn("batch_id", dbutils.widgets.get("batch_id", "default_batch_id"))
    
    # Write to the target table
    df.write.format("delta").saveAsTable("dea_accelerator.silver.customer")

# Make the job idempotent and re-runnable
# If the target table already exists, this will append to it
# If the target table does not exist, this will create it
load()