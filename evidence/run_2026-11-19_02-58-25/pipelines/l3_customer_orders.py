from pyspark.sql import SparkSession, functions as F
from pyspark.sql.types import StructType, StructField, StringType, IntegerType, DecimalType, DateType

# Define the build function
def build_customer_orders_layer(spark: SparkSession, customer_df: SparkSession, orders_df: SparkSession) -> SparkSession:
    # Merge the customer and orders DataFrames on the business key
    merged_df = customer_df.merge(orders_df, 'CustomerID', 'CustomerID')
    
    # Apply transformations to each column
    merged_df = merged_df.withColumn('customer_id', F.col('CustomerID'))
    merged_df = merged_df.withColumn('customer_id', F.col('CustomerID').cast('int'))
    merged_df = merged_df.withColumn('first_name', F.trim(F.col('FirstName')))
    merged_df = merged_df.withColumn('last_name', F.trim(F.col('LastName')))
    merged_df = merged_df.withColumn('email', F.lower(F.col('Email')))
    merged_df = merged_df.withColumn('phone', F.trim(F.col('Phone')))
    merged_df = merged_df.withColumn('city', F.trim(F.col('City')))
    merged_df = merged_df.withColumn('state_province', F.trim(F.col('StateProvince')))
    merged_df = merged_df.withColumn('country_code', F.trim(F.col('CountryCode')))
    merged_df = merged_df.withColumn('created_date', F.col('CreatedDate'))
    merged_df = merged_df.withColumn('modified_date', F.col('ModifiedDate'))
    merged_df = merged_df.withColumn('order_count', F.sum(F.col('COUNT(*)')).over())
    merged_df = merged_df.withColumn('total_order_amount', F.sum(F.col('SUM(SalesAmount)')).over())
    
    # Select only the target columns
    target_df = merged_df.select(
        'customer_id',
        'customer_id',
        'first_name',
        'last_name',
        'email',
        'phone',
        'city',
        'state_province',
        'country_code',
        'created_date',
        'modified_date',
        'order_count',
        'total_order_amount'
    )
    
    return target_df

# Define the load function
def load(spark: SparkSession) -> None:
    # Read the source DataFrames
    customer_df = spark.read.format("delta").load("dea_accelerator.silver.customer")
    orders_df = spark.read.format("delta").load("dea_accelerator.silver.orders")
    
    # Call the build function
    target_df = build_customer_orders_layer(spark, customer_df, orders_df)
    
    # Write the target DataFrame to the target table
    target_df.write.format("delta").saveAsTable("dea_accelerator.gold.customer_orders")

# Define the main function
def main() -> None:
    # Get the Spark Session
    spark = SparkSession.builder.getOrCreate()
    
    # Load the data
    load(spark)

# Define the audit columns
def add_audit_columns(df: SparkSession, batch_id: str) -> SparkSession:
    # Add the ingestion timestamp
    df = df.withColumn('ingestion_timestamp', F.current_timestamp())
    
    # Add the source file
    df = df.withColumn('source_file', F.input_file_name())
    
    # Add the batch ID
    df = df.withColumn('batch_id', batch_id)
    
    return df

# Define the main function with audit columns
def main_with_audit_columns() -> None:
    # Get the Spark Session
    spark = SparkSession.builder.getOrCreate()
    
    # Get the batch ID
    batch_id = dbutils.widgets.get('batch_id', 'default_batch_id')
    
    # Load the data
    load(spark)
    
    # Add the audit columns
    target_df = add_audit_columns(target_df, batch_id)
    
    # Write the target DataFrame to the target table
    target_df.write.format("delta").saveAsTable("dea_accelerator.gold.customer_orders")

# Define the main function with audit columns and idempotence
def main_with_audit_columns_and_idempotence() -> None:
    # Get the Spark Session
    spark = SparkSession.builder.getOrCreate()
    
    # Get the batch ID
    batch_id = dbutils.widgets.get('batch_id', 'default_batch_id')
    
    # Load the data
    load(spark)
    
    # Add the audit columns
    target_df = add_audit_columns(target_df, batch_id)
    
    # Check if the target table already exists
    if spark.catalog.tableExists("dea_accelerator.gold.customer_orders"):
        # If it exists, append to it
        target_df.write.format("delta").mode("append").saveAsTable("dea_accelerator.gold.customer_orders")
    else:
        # If it doesn't exist, overwrite it
        target_df.write.format("delta").mode("overwrite").saveAsTable("dea_accelerator.gold.customer_orders")

# Call the main function with audit columns and idempotence
main_with_audit_columns_and_idempotence()